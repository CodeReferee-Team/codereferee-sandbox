# kind 실제 Chaos 관측 샘플

2026-10-07 로컬 kind에서 실행한 결과 중 유효한 시나리오만 추출했다.
합성 데이터나 합격/불합격 라벨이 아니다. `exitCode: 0`은 실험 실행·복구 확인 결과이며,
서비스의 SLO 합격을 보장하지 않는다. AI 팀이 원본 probe와 토폴로지로 평가 라벨을 정해야 한다.

각 JSON은 응답에서 metrics, baseline metrics, observation, source, 원본 HTTP probes,
Litmus verdict/phase를 추출한 샘플 포맷이며 Sandbox API 전체 응답과는 다르다.
배포 manifest, 로그인 토큰, 비밀번호, HTTP 요청/응답 body는 포함하지 않았다.

| 시나리오 | 대상 | 실패/관측 요청 | p95 ms | 관측 복구 초 |
|---|---|---:|---:|---:|
| Container Kill | QuickByte | 22/59 | 124.66 | 78.02 |
| CPU Stress | QuickByte | 0/44 | 89.06 | 0 |
| Network Latency | QuickByte | 0/44 | 614.41 | 0 |
| Packet Loss | QuickByte | 0/43 | 1035.32 | 0 |
| Memory Pressure | QuickByte | 0/46 | 37.33 | 0 |
| DB Dependency | QuickByte | 4/27 | 2005.63 | 15.76 |
| Redis Dependency | QuickByte | 6/25 | 2107.29 | 23.86 |
| Memory OOM | fixture-api | 1/27 | 11.46 | 2.55 |
| Pod Delete | fixture-api | 1/45 | 15.97 | 2.54 |
| Service Blackhole | fixture-api | 15/30 | 14.25 | 13.55 |

## 출처와 제외 기준

- Container Kill: `quickbyte-runtime-standard-001.json`의 첫 시나리오만 추출.
  이후 관측 연결 오류로 전체 묶음은 실패했으므로 전체 요청 성공 샘플로 쓰지 않는다.
- CPU/Latency/Loss/Memory Pressure: `quickbyte-runtime-all-002.json`의 해당 시나리오만 추출.
  같은 파일의 초기 SIGSTOP 기반 DB/Redis 실험은 효과가 검증되지 않아 제외했다.
- DB/Redis: `frontend-backend-dependencies-004.json`, Backend task
  `05eeb7f9-ec38-4334-9d3e-75ab891163a3`. 전체 Redis 왕복 및 정리까지 확인했다.
  이전 `dependencies-003`은 workflow timeout 집계 결함이 있었으므로 제외했다.
- OOM: `kind-memory-limit-002.json`. 이번 실험 이후 OOMKilled와 restart 증가 확인.
- Pod Delete: `kind-pod-delete-observer-002.json`.
- Service Blackhole: `kind-routing-observer-001.json`. cluster 내부 HTTP로 단절·복구 확인.

## 해석 제한

- 단일 머신, 작은 표본, 낮은 요청량이다. 운영 신뢰성이나 처리 용량을 보장하지 않는다.
- QuickByte와 fixture를 같은 토폴로지로 취급하지 않는다. replica는 observation에 있다.
- 복구 시간 0은 실패 요청이 표본에 없었다는 뜻이지 성능 저하가 없었다는 뜻이 아니다.
- p95는 시나리오별 표본으로 계산했다. 묶음 p95로 최댓값을 사용하지 않는다.
- CPU/메모리 사용량 null은 미측정이다. fault 강도로 실제 사용량을 대체하지 않는다.
- 같은 실행의 probe들을 학습/평가 양쪽에 나눠 데이터 누수가 생기지 않게 한다.
- 전체 실행 응답과 로그는 로컬 `.runtime/evidence`에 보존한다.
