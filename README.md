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
| A2A 인프라 저장소 | 운영: PostgreSQL (`DATABASE_URL`), 로컬·Contract Test: SQLite 파일 |

Agent Card는 `GET /.well-known/agent-card.json`에서 공개합니다. Card의 `capabilities.streaming`은 `true`이며, SDK가 생성한 Route 중 MVP allowlist만 등록합니다.

```text
GET  /.well-known/agent-card.json
POST /a2a/message:send
POST /a2a/message:stream
GET  /a2a/tasks/{id}
POST /a2a/tasks/{id}:cancel
POST /a2a/tasks/{id}:subscribe
GET  /health/live
GET  /health/ready
```

Push notification, task list, extended card Route와 SDK 호환용 `GET /a2a/tasks/{id}:subscribe` 변형은 공개하지 않습니다. `POST /a2a/tasks/{id}:subscribe`만 MVP Subscribe 계약으로 허용합니다.

## 로컬 실행

```powershell
$env:WORKMATE_SERVICE_TOKEN = 'local-development-token'
uv sync --frozen
uv run python -m app.server
```

Windows에서 PostgreSQL Task Store를 사용할 때도 위 명령을 사용한다. 직접
`uvicorn app.main:app`을 실행하면 기본 Proactor Event Loop와 psycopg async
연결이 호환되지 않는다. 주소와 포트는 `WORKMATE_HOST`, `WORKMATE_PORT`로
변경한다.

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

M5.1 Workmate A2A 호환성 검수는 실행 중인 Agent를 실제 HTTP로 호출한다. 운영 Orchestrator가 없어도 Workmate의 Agent Card·5개 Skill·Artifact·Mock 차단을 먼저 확인할 수 있다. 이 결과는 M5.1 사전 호환성 증빙이며 실제 Orchestrator 종단 간 완료를 대신하지 않는다.

```powershell
$env:WORKMATE_A2A_BASE_URL = 'http://127.0.0.1:8001/a2a'
$env:WORKMATE_SERVICE_TOKEN = '로컬 토큰'
uv run python tools/m51_a2a_compatibility.py
```

테스트는 Agent Card의 HTTP+JSON·Streaming 선언, SDK Route allowlist, Bearer Token·`A2A-Version` 검사, SDK Message 직렬화, 승인된 Skill·Artifact Schema 검증, `message:send`·Streaming 응답을 확인합니다. Schema는 Superproject의 `docs/schemas/`를 자동 탐색하며, 별도 checkout에서는 `WORKMATE_SCHEMA_ROOT`로 지정합니다.

## 구현 경계

- `app/main.py`: FastAPI 진입점, health와 인증 미들웨어
- `app/a2a/runtime.py`: Agent Card, SDK `DefaultRequestHandler`, `AgentExecutor`, Route allowlist, Store·Registry 연결
- `app/a2a/persistence.py`: A2A Task·Message 멱등성·Artifact·`task_id ↔ thread_id`·Checkpoint 저장 경계
- `app/workflows/registry.py`: `skill_id → Workflow` 선택과 전송 독립 요청·결과 타입
- `migrations/001_a2a_infrastructure.sql`: PostgreSQL 운영용 M0.1 A2A 인프라 Migration
- `pyproject.toml`, `uv.lock`: 의존성의 단일 원장
- `tests/test_runtime_boot.py`: M0.1-01 부트스트랩 검증
- `tests/test_persistence.py`, `tests/test_migration.py`: M0.1-03 영속 경계·Migration 검증

현재 Executor는 Registry를 통해 런타임 준비 Workflow를 선택하고 A2A 인프라 Snapshot·멱등성·Checkpoint를 기록합니다. 이 준비 Artifact는 업무 결과가 아니며, 실제 Gmail·Calendar·업무 DB·LLM Workflow는 후속 M1~M4에서 연결합니다. `DATABASE_URL`이 없을 때만 로컬·Contract Test용 SQLite 파일을 사용하고, 운영 Compose는 PostgreSQL URL을 주입해야 합니다.

기존 Legacy `smoke_test.py`는 제거했으며, 공식 SDK 타입과 승인된 Contract Test로 교체했습니다. 실제 업무 Workflow와 Workmate Result 생성은 후속 M0.1-03 이후 범위입니다.
