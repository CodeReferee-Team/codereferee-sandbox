# Procfile 기반 Python 서비스 탐색

등록된 profile, validation.yaml, Compose, Dockerfile이 없는 경우 Procfile의
단일 `web:` 선언을 기존 스택 추론보다 먼저 확인합니다.

지원 예:

```text
web: gunicorn autoapp:app -b 0.0.0.0:$PORT -w 3
web: uvicorn main:app --host 0.0.0.0 --port $PORT
```

두 줄을 동시에 쓰라는 뜻이 아니라 각기 독립적인 예입니다.
이 자동 배포 경로는 루트 `requirements.txt`가 있어야 하며 이미지 빌드 중 설치합니다.
없으면 ConfigurationRequired로 반환합니다. pyproject/Poetry 등 다른 설치 방식은
Dockerfile과 validation.yaml로 명시해야 합니다. 기본 PORT는 8000입니다.
파일이 있다는 사실만으로 실행기와 모든 의존성이 포함됐다고 보장하지는 않습니다.
Gunicorn/Hypercorn의 bind, Uvicorn/Flask의 host와 port, Daphne의 bind와 port,
Waitress의 listen 또는 host와 port 선언을 확인합니다. `python -m` 실행은
gunicorn/uvicorn/hypercorn/flask 모듈만 지원합니다. waitress-serve 같은 console
script 이름을 Python 모듈로 간주하지 않습니다.

포트가 주석이나 프로세스 이름에만 등장하는 경우, PORT 재정의, loopback 바인딩,
여러 web 선언, 복합 shell 명령, 임의 Python 스크립트는 설정을 요구합니다.
생성 이미지에서 준비하지 않는 실행기 경로(.venv/bin 등), 환경변수 대입,
주석/리다이렉션, PORT 바인딩 외 변수 확장, Unix socket/fd, daemon 또는
설정 출력 후 종료, TLS 실행 모드도 자동 배포 대상에서 제외합니다.
이 경로는 포그라운드 plain HTTP/TCP 서비스에 한정합니다.
validation.yaml로 생성 이미지를 사용할 때 env.PORT와 생성 포트가 충돌하면
설정을 요구합니다. 같은 PORT 값은 허용하며 직접 작성한 Dockerfile은 이
생성 이미지 전용 확인 조건의 대상이 아닙니다.
이때 `.codereferee/validation.yaml`과 Dockerfile로 실행 환경을 명시합니다.
Ruby/Node Procfile은 이 Python 기능의 대상이 아니며 기존 탐색 경로를 유지합니다.

실행 선언 탐색 성공은 빌드·기동 성공을 의미하지 않습니다. Python 버전,
의존성, 외부 시스템 설정과 실제 HTTP 응답은 후속 실행 단계에서 검증합니다.
