# 설정 파일 기반 멀티레포 검증

기준 레포의 `.codereferee/validation.yaml`을 version 3으로 작성한다. 기존 제출 화면에는 이 기준 레포 URL을 입력한다. Sandbox가 설정에 선언한 다른 레포를 복제하고 하나의 서비스 그룹으로 검증한다.

## 구성 예시

다음 예시는 FE가 기준 레포이고 API가 다른 레포인 구성이다. API의 환경 변수와 probe 경로는 실제 앱에 맞게 작성해야 한다.

```yaml
version: 3
repositories:
  backend:
    url: https://github.com/example/backend.git
    branch: main
    # commitSha: 전체 40자리 커밋 SHA를 지정할 수도 있다
services:
  web:
    buildContext: .
    proxyRoutes:
      /api/:
        service: api
    resources: {cpu: 500m, memory: 128Mi}
  api:
    repository: backend
    buildContext: .
    port: 8000
    healthPath: /health
    resources: {cpu: 1000m, memory: 256Mi}
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
```

Vite 정적 FE의 `proxyRoutes`와 `browserProbe`, 업무 `probeSpec`은 version 2와 같은 형식이다. 실제 업무를 검증하려면 서비스가 제공하는 요청·응답에 맞게 probe를 선언한다. 브라우저 업무와 HTTP 장애 중 연속 측정의 범위는 [FE·API·DB 업무 검증](browser-business-validation.md)을 따른다.

## 소스와 배포

- `main`은 제출한 기준 레포다. 서비스의 `repository`를 생략하면 main을 사용한다.
- `repositories`에 추가 레포 1~4개를 선언한다. 서비스는 기존 범위인 2~5개다. 한 레포가 여러 서비스를 제공할 수 있다.
- 추가 레포마다 `url`, 선택적인 `branch`, `commitSha`를 지정한다. URL은 기존과 같은 GitHub HTTPS 형식이다.
- `buildContext`, `dockerfile`, `verificationDirectory`는 해당 서비스가 사용하는 레포 기준의 상대 경로다. 다른 레포나 요청 작업 폴더 바깥을 경로로 참조할 수 없다.
- 별도 설정이 없는 지원 스택은 기존 자동 탐색으로 실행 방법을 결정한다. 모호하면 Dockerfile 등 명시적인 설정이 필요하다.
- 각 서비스의 검사를 수행하고 이미지를 빌드한 뒤 기존 `dependsOn` 순서로 배포한다. DB·Redis는 요청별로 생성한 재현 환경을 사용한다.
- version 3의 최상위 `verification`은 기준 레포 검사에 적용한다. 다른 레포 검사를 대신하지 않는다. 추가 레포는 자신의 설정 또는 자동 검사 경로를 사용한다.

각 소스를 동일한 요청 작업 폴더 안의 서로 다른 디렉터리에 복제한다. 실행은 하나의 Sandbox·Kubernetes namespace에 배치한다. 여러 EC2로 나누는 기능은 후속 범위다.

기준 레포에 설정 파일만 두고 FE·BE를 모두 추가 레포로 지정할 수도 있다. 이 경우 두 서비스에 각각 `repository`를 지정한다. 앱 소스를 수정하기 어려울 때 사용할 수 있는 구성이다.

## 실행 근거와 정리

응답의 `source.repositories`에 이름별 URL·브랜치·실제 커밋 SHA를, `source.service_repositories`에 서비스별 소스 이름을 제공한다. 이미지 태그도 각 소스의 실제 SHA를 사용한다. 서비스별 검사·빌드·배포 결과는 기존 `sandboxReport.services`, `service_timings`에 남는다.

추가 레포 복제 실패에는 `failed_repository`, 서비스 검사·빌드·배포 실패에는 `failed_service`를 기록한다. 배포 실패의 Pod 진단은 기존 경로를 사용한다. 준비 재시도에서는 이미 확인한 각 레포 커밋을 고정한다. 브랜치를 사용한 새 검증 요청은 새 커밋을 받을 수 있다.

중간 실패와 정상 종료 모두 요청 작업 폴더 전체를 정리한다. 요청 namespace 삭제 확인 후 호스트·kind 이미지를 회수하며, 프로세스 중단 시 기존 복구 목록으로 잔여 자원을 회수한다.

## 현재 범위와 후속

- 여러 레포 URL만으로 서비스 관계를 추측하는 UI는 후속이다. 이번에는 설정 파일로 관계를 선언한다.
- 기존 `patchDiff`는 기준 레포에 적용한다. 추가 레포는 `repositoryPatches`와 `repositoryCommits`로 기준 SHA와 누적 패치를 전달한다. [패치 계약](multi-repository-patches.md)을 참고한다. AI 연결 및 실측 범위는 해당 검증 기록을 별도로 확인한다.
- 여러 레포의 커밋 조합을 기준으로 하는 이력 조회와 결과 비교 화면도 후속이다.
- 멀티레포 자동 탐색, 여러 Sandbox 배정, EC2 자동 기동·종료는 별도 과제로 유지한다.
