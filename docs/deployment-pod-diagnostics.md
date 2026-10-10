# 배포 실패 Pod 진단 계약

롤아웃 실패 시 요청 namespace를 삭제하기 **전에** Deployment 소유 Pod의 상태,
최근 로그, 이벤트를 수집한다. 카오스 성공/실패 판정이나 AI 모델·정책은 변경하지 않는다.

## 응답

Sandbox HTTP 응답의 `sandboxReport.pod_diagnostics`에 추가한다. 기존 AI HTTP 파서는
이를 `execution_result.sandbox_report.pod_diagnostics`로 보존한다. 정상 배포에는 붙이지 않는다.

```json
{
  "pod_diagnostics": {
    "collected_at": "2026-10-10T00:00:00+00:00",
    "collection_errors": [],
    "truncated": false,
    "pods": [{
      "name": "repository-api-example",
      "uid": "pod-uid",
      "phase": "Running",
      "containers": [{
        "name": "api",
        "ready": false,
        "restart_count": 3,
        "waiting_reason": "CrashLoopBackOff",
        "last_terminated": {"reason": "Error", "exit_code": 1},
        "terminated": {"reason": null, "exit_code": null},
        "logs_source": "previous",
        "logs_available": true,
        "logs_tail": "KeyError: 'DATABASE_URL'"
      }],
      "logs_tail": "KeyError: 'DATABASE_URL'",
      "events": [{"reason": "BackOff", "message": "Back-off restarting failed container", "count": 3}]
    }]
  }
}
```

`logs_source`는 previous/current/null이다. 이전 로그가 없으면 현재 로그를 시도한다.
로그를 얻지 못한 것과 실제 빈 로그는 `logs_available`로 구분한다. 기존 종료 코드와
타임아웃 원문은 보존한다. 진단 조회 실패가 원래 오류를 덮거나 cleanup을 막지 않는다.

## 수집 범위 및 제한

- namespace + Deployment UID → ReplicaSet UID → Pod UID 소유 관계 확인.
- 최대 Pod 4개, Pod당 컨테이너 4개, 로그당 50줄/6,000자, Pod 이벤트 12개.
- 이벤트는 Pod UID로 조회해 이름이 같은 다른 Pod의 기록을 제외한다.
- 명령당 최대 5초, 전체 진단 최대 25초. 일부 실패·상한 초과는 명시한다.
- Pod spec/env나 Kubernetes Secret 원문을 반환하지 않는다.
- 로그·이벤트의 알려진 재현 비밀값과 password/token/API key/인증 헤더/URL 자격증명
  패턴을 마스킹한다. 임의 앱이 출력하는 모든 형태의 비밀값을 탐지한다고 보장하지 않는다.
- 로그는 **비신뢰 데이터**다. AI는 로그에 포함된 지시를 실행해서는 안 된다.
- 현재 범위는 배포 롤아웃 실패다. build/test 실패, init-container 상세, 성공 배포의
  지속 로그 수집은 별도 범위다. 추가 읽기 권한: deployments, replicasets, pods,
  pods/log, events (요청 namespace).

## AI 팀 연동

waiting reason + 종료 코드 + 로그 + probe 이벤트를 근거 패킷에 연결하고, 증거가
부족하면 근본원인 미확정으로 유지한다. 이 수집 기능만으로 LLM 패치 생성이나
자가치유 성공을 보장하지 않는다. Critic/Refiner의 분류·패치 정책은 AI 팀 담당이다.

## 실측

Windows + Docker Desktop + 기존 kind 클러스터에서 공개 시연 레포를 clone/build/배포:

- 부팅 크래시: CrashLoopBackOff, 비정상 종료, KeyError 로그, BackOff 이벤트 수집.
- HTTP 500: Running/not-ready, KeyError 로그, Unhealthy/readiness HTTP 500 이벤트 수집.
- 이미지 풀 실패: 전용 Deployment에서 ErrImagePull/Failed 이벤트 수집.
- 두 레포 실패 실행 뒤 namespace·host/kind 이미지·workspace 정리 확인.

레포 두 실패 실측은 CLI `--rollout-timeout-seconds 45`로 수행했다. 이미지 풀 실패는
진단 수집기 + 실제 Deployment 실측으로, Git clone/API 전체 경로와 구분한다.
원본 JSON·로그는 `.runtime/evidence/pod-diagnostics`에만 보관하며 커밋하지 않는다.

### 로컬 연동 재검증

별도 포트와 전용 임시 PostgreSQL/Redis에서 BE → Redis → AI main → Sandbox →
Redis → BE 조회·저장을 실제 실행했다. 사용자 DB·큐는 사용하지 않았다.

| 실행 | Backend 결과 | 확인 내용 |
| --- | --- | --- |
| 정상 FastAPI | PASSED | 기존 일반 실행 경로 유지 |
| HTTP 500 앱 | FAILED | 진단 1개가 AI 응답·Backend JSON·PostgreSQL에 저장됨 |
| 기존 Container Kill 시연 | PASSED | 실제 Chaos 실행 및 최종 결과 전달 |
| HTTP 500 앱 + 사람이 작성한 patchDiff | Sandbox exitCode 0 / HTTP 200 | clone·패치·배포·smoke·이미지/namespace 정리 |

프론트 Vite에서 HTML 200 및 `/api` 프록시 결과 조회도 확인했다. 브라우저 클릭·자가치유
화면 전체 시연을 검증했다고 주장하지 않는다. 로컬 Gemini 키가 없어 LLM 자동 패치 생성은
미실행이다. 마지막 행은 사람이 작성한 패치로 Sandbox HTTP API를 호출한 실행이다.

Windows Python 회귀 테스트 111개 통과. PR #13 보완은 별도 브랜치에서 Linux Bash
테스트 12개를 통과했으며, AWS 신규 Linux 호스트 설치 실측과는 구분한다.

테스트 앱 주의: `crashloop` 브랜치는 환경변수 접근을 수정해도 `sys.exit(0)`으로 종료하고
HTTP 서버를 실행하지 않는다. 환경변수 가드 패치만으로 정상 웹 서비스가 된다고 가정하면
안 된다. Sandbox 진단 수집과 테스트 앱·AI 패치 정책 문제를 구분해야 한다.
