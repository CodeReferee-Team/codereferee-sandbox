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
