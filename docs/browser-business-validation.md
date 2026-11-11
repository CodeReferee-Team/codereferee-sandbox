# FE·API·DB 업무 검증

단일 레포의 version 2 서비스 그룹에서 FE와 API를 연결하고, 선언된 브라우저 업무를 실행한다. 앱별 화면과 업무 경로는 레포의 `.codereferee/validation.yaml`에 작성한다.

## 실행 순서

1. 기존 서비스별 검사·빌드·배포를 수행한다.
2. 실제 Chromium으로 FE를 열고 입력·클릭·응답·화면 내용을 확인한다.
3. 초기 업무가 성공한 경우 요청한 장애·부하 실험을 실행한다.
4. 각 실험 후 새 브라우저 세션에서 같은 업무를 다시 확인한다.
5. 결과를 확보하고 브라우저 Pod, 요청 namespace, 요청 이미지와 작업 폴더를 정리한다.

장애 중 연속 측정은 `probeSpec`의 HTTP 업무 요청이다. 브라우저는 장애 전후의 확인 지점에서 실행한다. 부하의 동시 사용자 수도 HTTP 작업 기준이며 Chromium 인스턴스 수가 아니다.

## 실행 환경 준비

전용 런타임 이미지로 브라우저 Pod를 실행한다. Windows와 Mac/Linux에서 같은 Python 명령을 사용한다.

```console
python scripts/build_browser_runtime.py --kind-cluster codereferee
```

PATH에 kind가 없다면 `CODEREFEREE_KIND_COMMAND`에 설치한 실행 파일을 지정한다. 명령은 이미지를 빌드해 kind 노드에 로드하고 Chromium용 seccomp 프로필을 모든 노드에 설치한다. API를 시작하는 셸이나 서비스 환경에 아래 값을 설정한다.

PowerShell에서는 다음 명령을 사용한다.

```powershell
$env:CODEREFEREE_BROWSER_SECCOMP_PROFILE = 'codereferee-browser.json'
```

Bash에서는 다음 명령을 사용한다.

```bash
export CODEREFEREE_BROWSER_SECCOMP_PROFILE=codereferee-browser.json
```

일반 Kubernetes에서는 운영자가 런타임 이미지를 레지스트리에 게시하고 `CODEREFEREE_BROWSER_IMAGE`로 지정한다. 해당 노드의 kubelet seccomp 디렉터리에 프로필도 설치해야 한다. 프로필 설정을 생략하면 `RuntimeDefault`를 사용하며, Chromium 격리를 지원하지 않는 노드에서는 실행 오류를 반환한다.

Chromium 자체 격리를 활성화하며 non-root, 읽기 전용 root filesystem, capability 제거, 서비스 계정 토큰 비마운트, CPU·메모리 및 실행 시간 제한을 사용한다. 브라우저 HTTP 요청은 업무 진입 Service와 같은 origin으로 제한하고 service worker와 WebSocket은 차단한다. 외부 인증·결제 페이지 연동은 후속 과제다.

`browser/seccomp.json`은 Playwright v1.64.1의 Docker 프로필을 그대로 보관한 것이다. SHA-256은 `cc3e61cabda6bbc1e53e54d27ba4d55a9d3be829b6dd1a596f4a7b31b1cc7849`이다. 기본 seccomp 정책에 Chromium user namespace에 필요한 호출 허용을 포함한다. [공식 Docker 안내](https://playwright.dev/python/docs/docker), [고정 버전 프로필](https://github.com/microsoft/playwright/blob/v1.64.1/utils/docker/seccomp_profile.json)

## 설정 예시

```yaml
version: 2
services:
  web:
    buildContext: web
    proxyRoutes:
      /api/:
        service: api
        stripPrefix: false
  api:
    buildContext: api
    healthPath: /health
    env:
      DB_HOST: '{{dependency.database.host}}'
      DB_PORT: '{{dependency.database.port}}'
      DB_USER: '{{dependency.database.username}}'
      DB_PASSWORD: '{{dependency.database.password}}'
      DB_NAME: '{{dependency.database.database}}'
dependencies:
  database:
    type: postgres
entryService: web
targetService: api
probePath: /api/items
probeSpec:
  setup:
    - path: /api/items
      method: POST
      body: {label: 'http-${runId}'}
      extract: {itemId: id}
  steps:
    - path: '/api/items/${itemId}'
      expect: {saved: true, label: 'http-${runId}'}
browserProbe:
  timeoutSeconds: 30
  stepTimeoutSeconds: 5
  steps:
    - {action: goto, path: /}
    - {action: fill, selector: '#item', value: 'ui-${runId}'}
    - action: click
      selector: '#save'
      expectResponse:
        path: /api/items
        status: 201
        expect: {saved: true}
    - {action: assertText, selector: '#items', text: 'ui-${runId}'}
    - {action: goto, path: /}
    - {action: assertText, selector: '#items', text: 'ui-${runId}'}
```

`proxyRoutes`는 생성된 Vite 정적 배포에만 적용된다. `/api/`를 선언된 API Service로 전달하고 API가 FE보다 먼저 배포되도록 의존 순서를 추가한다. 기본값은 `/api/items` 경로를 유지한다. `stripPrefix: true`이면 API에 `/items`로 전달한다. 자체 Dockerfile을 사용하는 FE는 프록시를 직접 구성한다.

브라우저 단계는 2~16개, 전체 5~60초, 단계별 1~15초로 제한한다. `goto`, `fill`, `click`, `assertVisible`, `assertText`를 지원한다. 첫 단계는 `goto`이고 화면 확인 단계를 하나 이상 포함해야 한다. 클릭에는 선택적으로 HTTP 응답 상태와 작은 JSON 필드 검사를 선언할 수 있다. `${runId}`는 한 업무 실행 동안 유지되는 임의 ID다.

DB 장애 시에도 업무 API를 측정하려면 의존성의 `probePath`·`probeSpec`을 별도로 선언한다. 실행 가능한 전체 예시는 `tests/fixtures/browser-business/.codereferee/validation.yaml`에 있다.

## 결과와 실패 분류

- `source.browser_validation.checks`에 확인 단계, 완료 단계 수, 실패 단계, HTTP 상태 집계, 오류 코드를 제공한다.
- 업무 단계가 실패하면 `observed`, `exitCode: 1`, `browserLoaded: false`로 전달한다.
- 런타임 이미지·브라우저 기동·Kubernetes 실행 문제는 `infrastructure_error`, `exitCode: null`이다.
- 검사 묶음의 일부만 실행 오류인 경우 기존 부분 결과 계약을 유지한다. 개별 시나리오 오류와 미실행 항목을 함께 확인해야 한다.
- 초기 업무가 성공한 뒤 장애 주입 단계로 진행한다.
- HTTP 복구 후 브라우저 업무가 실패하면 HTTP 실행 근거와 복구 시간을 `source.http_execution`에 보존한다. 전체 업무의 `chaos_observation.recovered`는 false, `recovery_seconds`는 null, `recovery_basis`는 `http_and_declared_browser_workflow`다.
- 기존 HTTP 실패나 인프라 오류를 우선 보존한다.

공개 브라우저 결과는 완료 단계 수, 실패 위치, HTTP 상태 집계와 오류 코드로 구성한다. 입력 값과 업무 설정은 요청 내부 계약으로 처리한다.

## 후속 과제

- 현재 AI 파서와 결정적 판정에서 브라우저 실패가 Fail로 유지되는 것을 확인했다. 기존 비정상 종료 설명은 이를 build 실패로 표현하기도 하므로 AI 측에서 `source.browser_validation`과 실패 단계를 사용해 설명을 구분해야 한다.
- 업무별 초기 데이터·외부 의존성 재현과 서비스별 영향 검증을 확장한다.
- 복구 중 브라우저 업무를 연속 측정하는 정책과 브라우저 부하 생성은 별도 설계한다.
- 긴 실행의 시간 연장·재시도, 멀티레포 구성, 여러 EC2 배정과 자동 기동·종료는 별도 작업이다.
- 런타임 이미지는 재사용하는 공유 설치 항목으로 관리한다. 버전 교체 시 운영자가 이전 버전을 회수한다.
