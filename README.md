# Workmate AI Agent

Workmate AI의 공식 `a2a-sdk==1.1.2` HTTP+JSON 런타임입니다. 현재 M0.1-01 범위는 Agent Card, 인증 미들웨어, SDK REST Handler, Streaming·Subscribe Route를 부트스트랩하는 단계이며 Gmail·Calendar·PostgreSQL·LLM Workflow는 이후 마일스톤에서 연결합니다.

## 현재 계약

| 항목 | 값 |
| --- | --- |
| 컨테이너 | `workmate-agent` |
| 포트 | `8001` |
| A2A Base URL | `http://workmate-agent:8001/a2a` |
| 인증 | `Authorization: Bearer $WORKMATE_SERVICE_TOKEN` |
| A2A 버전 | `1.0` (`A2A-Version` 헤더) |
| SDK | `a2a-sdk==1.1.2` |
| Protocol Binding | `HTTP+JSON` |

Agent Card는 `GET /.well-known/agent-card.json`에서 공개합니다. Card의 `capabilities.streaming`은 `true`이며, SDK가 생성한 Route 중 MVP allowlist만 등록합니다.

```text
GET  /.well-known/agent-card.json
POST /a2a/message:send
POST /a2a/message:stream
GET  /a2a/tasks/{id}
POST /a2a/tasks/{id}:cancel
GET  /a2a/tasks/{id}:subscribe
POST /a2a/tasks/{id}:subscribe
GET  /health/live
GET  /health/ready
```

Push notification, task list, extended card Route는 자동 공개하지 않습니다. `POST /a2a/tasks/{id}:subscribe`는 SDK가 제공하는 Subscribe의 공식 경로이며, GET 변형도 SDK 호환을 위해 허용합니다.

## 로컬 실행

```powershell
$env:WORKMATE_SERVICE_TOKEN = 'local-development-token'
uv sync --frozen
uv run uvicorn app.main:app --host 0.0.0.0 --port 8001
```

확인:

```powershell
Invoke-RestMethod http://localhost:8001/health/live
Invoke-RestMethod http://localhost:8001/.well-known/agent-card.json
```

## Docker 실행

`.env`에 Secret을 저장할 수 있지만 Git에는 커밋하지 않습니다.

```env
WORKMATE_SERVICE_TOKEN=local-development-token
APP_BASE_URL=http://workmate-agent:8001/a2a
```

```powershell
docker compose up --build -d
docker compose ps
docker compose down
```

## 테스트

M0.1-01과 M0.1-02의 기준 테스트는 `tests/` 아래의 공식 SDK 런타임·Contract Test입니다.

```powershell
uv run python -m unittest discover -s tests -v
```

테스트는 Agent Card의 HTTP+JSON·Streaming 선언, SDK Route allowlist, Bearer Token·`A2A-Version` 검사, SDK Message 직렬화, 승인된 Skill·Artifact Schema 검증, `message:send`·Streaming 응답을 확인합니다. Schema는 Superproject의 `docs/schemas/`를 자동 탐색하며, 별도 checkout에서는 `WORKMATE_SCHEMA_ROOT`로 지정합니다.

## 구현 경계

- `app/main.py`: FastAPI 진입점, health와 인증 미들웨어
- `app/a2a/runtime.py`: Agent Card, SDK `DefaultRequestHandler`, `AgentExecutor`, Route allowlist
- `pyproject.toml`, `uv.lock`: 의존성의 단일 원장
- `tests/test_runtime_boot.py`: M0.1-01 부트스트랩 검증

현재 Executor는 업무 Artifact를 생성하지 않고 런타임 연결을 확인하는 최소 응답만 반환합니다. 승인된 Skill·Artifact Schema와 실제 Gmail·Calendar·DB·LLM Workflow는 M0.1-02 이후에 연결하며, 이 단계의 완료를 업무 기능 완료로 해석하지 않습니다.

기존 Legacy `smoke_test.py`는 제거했으며, 공식 SDK 타입과 승인된 Contract Test로 교체했습니다. 실제 업무 Workflow와 Workmate Result 생성은 후속 M0.1-03 이후 범위입니다.
