# Mac/Linux 로컬 Sandbox 준비

Docker와 kubectl이 설치·실행 가능한 환경에서 Bash로 실행한다. 시스템 kind 설치는 필수가 아니다.

```bash
# source로 실행해야 이후 API가 같은 kind/context 설정을 사용한다.
source scripts/bootstrap_local.sh codereferee
python -m uvicorn app.main:app --host 127.0.0.1 --port 8102
```

`bash scripts/bootstrap_local.sh`로 실행하면 설치는 가능하지만 export된 설정이 부모 터미널에 남지 않는다.
Windows에서는 기존 PowerShell 스크립트를 유지해 사용한다. `.ps1`을 제거하지 않는다.

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

### Windows Git Bash 실측 (2026-10-08)

LF 줄바꿈 checkout에서 Git Bash로 `source scripts/bootstrap_local.sh codereferee`를 실행했다.
기존 kind 및 클러스터를 재사용했고 실제 고정 매니페스트 다운로드·검증·apply, operator Ready,
6종 실험 등록과 export된 provider/context를 확인했다. 새 클러스터 생성 및 Mac kind 바이너리
설치를 다시 검증한 것은 아니다. 일반 Windows 사용자는 기존 PowerShell 경로를 사용한다.

등록된 6종은 전용 `fixture-api`에 각각 5초, baseline HTTP probe 5회 조건으로 순차 주입한다.
결과는 원본 Litmus verdict와 독립 HTTP 복구 관측을 확인한다. 이는 가벼운 설치·실행 경로 검증이며
실서비스 SLO 합격, 부하 테스트, 정밀 검사 묶음 전체 또는 Backend/AI E2E 검증을 뜻하지 않는다.
Windows 실제 결과와 Mac/Linux 재검증 요청은 PR #9 코멘트에 기록한다.
