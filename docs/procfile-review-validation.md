# PR #17 보완 검증 (2026-10-12)

기존 구현은 `$PORT` 문자열의 존재만 확인해서 주석, 다른 옵션, PORT_SUFFIX,
PORT 재정의, loopback, single-quoted PORT, 중복 web 선언을 받아들였습니다.
추가한 7개 거부 테스트는 수정 전 모두 실패했고 수정 후 통과했습니다.

## 검증 결과

- Windows Python 회귀 테스트: 136개 실행, 성공, 1개 건너뜀.
- POSIX 테스트: Windows 직접 실행은 셸 차이로 실패하여 결과를 분리했습니다.
  WSL Ubuntu에서 Bash 부트스트랩 12개 테스트 모두 통과했습니다.
- Procfile Gunicorn/Flask fixture를 resolve_plan으로 탐색했습니다.
  생성 Dockerfile과 실제 빌드 입력의 동일성을 확인한 뒤 Docker Desktop Linux에서
  이미지 빌드·기동, HTTP 200과 JSON 응답을 확인했습니다.
- 해당 실행 전용 컨테이너와 이미지 태그는 제거했습니다. 공유 이미지나 빌드 캐시는 삭제하지 않았습니다.

이번 실제 실행은 선언 탐색 → 이미지 빌드 → HTTP 응답 검증입니다.
이 PR의 Kubernetes 배포·Chaos·Backend/AI 전체 E2E 및 모든 지원 runner의
실행 검증은 아닙니다. 공개 예제 레포의 오래된 의존성이 Python 3.12에서
동작한다는 보장도 하지 않습니다. 설정 탐색 성공과 실제 실행 성공은 구분합니다.
