# Mac/Linux 로컬 Sandbox 준비

Docker가 실행 중인 환경에서 Bash로 실행한다. kind와 kubectl은 없으면 부트스트랩이 받는다.

API 의존성은 별도로 설치한다. 아래는 Python 3.11이 설치된 Linux의 예다. Mac에서는
설치한 Python 3.10 이상 경로로 바꾼다. 선택된 Python이 가상환경을 가리키도록 명시한다.

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export CODEREFEREE_PYTHON="$PWD/.venv/bin/python"
```

부트스트랩의 Python 버전 확인은 uvicorn/FastAPI 설치까지 대신하지 않는다.

```bash
# source로 실행해야 이후 API가 같은 kind/context 설정을 사용한다.
source scripts/bootstrap_local.sh codereferee
"$CODEREFEREE_PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port 8102
```

`bash scripts/bootstrap_local.sh`로 실행하면 설치는 가능하지만 export된 설정이 부모 터미널에 남지 않는다.
Windows에서는 기존 PowerShell 스크립트를 유지해 사용한다. `.ps1`을 제거하지 않는다.

## 전제

| 필요한 것 | 없으면 |
|---|---|
| Docker (데몬 실행 중, 현재 사용자가 docker 그룹) | 부트스트랩이 안내하고 멈춘다 |
| kind | `install_kind.sh`가 받아서 `.runtime/tools`에 둔다 |
| kubectl | `install_kubectl.sh`가 받아서 `.runtime/tools`에 둔다 |
| **Python 3.10 이상** | 부트스트랩이 안내하고 멈춘다 |

`app/main.py`와 `app/scenarios.py`가 PEP 604 union(`X | None`)을 쓴다. 3.9에서는 uvicorn을 띄우는
순간이 아니라 **import 단계에서 TypeError로 깨진다.** Amazon Linux 2023의 기본 `python3`가 3.9라
새 호스트에서 바로 걸리므로, 부트스트랩이 쓸 수 있는 인터프리터를 먼저 찾아 `CODEREFEREE_PYTHON`으로
export한다. 없으면 설치 방법과 함께 중단한다.

```bash
sudo dnf install -y python3.11 python3.11-pip   # Amazon Linux 2023
```

특정 인터프리터를 쓰려면 `CODEREFEREE_PYTHON`을 먼저 설정하면 그 값을 우선한다. 3.10 미만이면 무시한다.

## source 로 실행할 때의 규칙

`bootstrap_kind.sh`와 `bootstrap_local.sh`는 source 되는 것을 전제로 쓰였다. 그래서 두 가지를 지킨다.

- **실패해도 `exit`하지 않는다.** source 상태의 `exit`는 호출한 셸을 그대로 종료시킨다. user-data에서는
  뒤 단계가 통째로 실행되지 않고, SSM 세션에서는 메시지 없이 세션이 끊겨 원인을 찾기 어렵다.
- **`set` 옵션을 호출한 셸에 남기지 않는다.** `-u`가 남으면 바로 뒤에 같은 셸에서 띄우는 uvicorn이
  미정의 변수 참조 한 번에 죽는다.

본문을 함수에 두고 실패를 명시적으로 검사하는 구조가 그 때문이다. 조건식에서 호출된 함수 안에서는
`errexit`가 적용되지 않으므로 거기에 기대지 않는다. 임시 변수 이름에 스크립트 이름을 붙인 것도
`bootstrap_local.sh`가 `bootstrap_kind.sh`를 dot-source할 때 안쪽이 바깥쪽 값을 지우지 않게 하기
위해서다. 이 동작들은 `tests/test_posix_bootstrap.py`가 고정한다.

## 등록되는 Litmus 실험

- pod-delete, container-kill
- pod-cpu-hog, pod-network-latency, pod-network-loss
- pod-memory-hog (OOM 시나리오도 같은 실험 정의를 사용)

Scale Down·Service Blackhole·Rollout은 별도 Litmus 실험 설치가 아닌 Kubernetes API 경로다.
kind와 Litmus 버전/커밋 핀은 기존 설치본을 유지한다. 캐시된 파일도 종류·이름·runner 버전을
검증한 뒤 적용하고, 다운로드 실패 파일은 정식 캐시로 남기지 않는다.

## 검증 범위

팀원이 Mac ARM64에서 기존 Pod Delete/Container Kill 설치까지 실제 완주한 기록은 PR #9에 있다.
추가된 실험 설치 순서·명시적 context·잘못된 캐시 거부·apply 실패 즉시 중단은 Bash를 실제
실행하되 kubectl/curl을 테스트 대역으로 바꿔 검증한다. 이것을 Mac/Linux 실제 클러스터의
추가 Chaos 실측으로 주장하지 않는다. 최신 실험 목록은 PowerShell 설치본과 회귀 테스트로 비교한다.

### 아직 실측하지 않은 것

**새 Linux 호스트에서 처음부터 완주한 기록이 없다.** 아래는 Amazon Linux 2023 컨테이너에서 확인한
범위다. 실제 EC2에서 클러스터 생성까지 포함한 완주는 별도 검증이 필요하다.

- 기본 `python3`가 3.9.25이고 `app/scenarios.py` import가 TypeError로 깨지는 것
- `kubectl`이 설치되어 있지 않은 것
- source 실패가 호출한 셸을 종료시키던 것과 `set` 옵션이 남던 것

### Windows Git Bash 실측 (2026-10-08)

LF 줄바꿈 checkout에서 Git Bash로 `source scripts/bootstrap_local.sh codereferee`를 실행했다.
기존 kind 및 클러스터를 재사용했고 실제 고정 매니페스트 다운로드·검증·apply, operator Ready,
6종 실험 등록과 export된 provider/context를 확인했다. 새 클러스터 생성 및 Mac kind 바이너리
설치를 다시 검증한 것은 아니다. 일반 Windows 사용자는 기존 PowerShell 경로를 사용한다.

등록된 6종은 전용 `fixture-api`에 각각 5초, baseline HTTP probe 5회 조건으로 순차 주입한다.
결과는 원본 Litmus verdict와 독립 HTTP 복구 관측을 확인한다. 이는 가벼운 설치·실행 경로 검증이며
실서비스 SLO 합격, 부하 테스트, 정밀 검사 묶음 전체 또는 Backend/AI E2E 검증을 뜻하지 않는다.
Windows 실제 결과와 Mac/Linux 재검증 요청은 PR #9 코멘트에 기록한다.
