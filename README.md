# Workmate Agent Shell

오케스트레이터와 Workmate AI 사이의 A2A 통신을 검증하기 위한 Mock Agent입니다. Gmail, Calendar, DB, LLM은 호출하지 않습니다.

## 계약

| 항목 | 값 |
| --- | --- |
| Docker 서비스명 | `workmate-agent` |
| 컨테이너 Port | `8001` |
| A2A Base URL | `http://workmate-agent:8001/a2a` |
| 인증 | Bearer Service Token |
| A2A 버전 | `1.0` |

지원 Operation:

```text
GET  /.well-known/agent-card.json
POST /a2a/message:send
GET  /a2a/tasks/{task_id}
POST /a2a/tasks/{task_id}:cancel
GET  /health/live
GET  /health/ready
```

지원 Mock Skill:

```text
daily_briefing
weekly_report
analyze_meeting
search_meetings
rank_priorities
```

## 단독 실행

`.env.example`을 `.env`로 복사하고 Token을 입력합니다.

```powershell
Copy-Item .env.example .env
```

```env
WORKMATE_SERVICE_TOKEN=팀에서-합의한-테스트-토큰
APP_BASE_URL=http://workmate-agent:8001/a2a
```

실행:

```powershell
docker compose up --build -d
docker compose ps
```

PC에서 확인:

```text
http://localhost:8001/.well-known/agent-card.json
http://localhost:8001/health/ready
```

종료:

```powershell
docker compose down
```

## Orchestrator Compose에 통합

두 저장소를 같은 상위 폴더에 clone합니다.

```text
ai-agent-platform/
├─ orchestrator/
│  └─ compose.yaml
└─ workmate-agent/
   └─ Dockerfile
```

오케스트레이터의 `compose.yaml`에 Workmate 서비스를 추가합니다.

```yaml
services:
  orchestrator:
    build: .
    environment:
      WORKMATE_AGENT_URL: "http://workmate-agent:8001/a2a"
      WORKMATE_SERVICE_TOKEN: "${WORKMATE_SERVICE_TOKEN}"
    depends_on:
      workmate-agent:
        condition: service_healthy

  workmate-agent:
    build:
      context: ../workmate-agent
    expose:
      - "8001"
    environment:
      WORKMATE_SERVICE_TOKEN: "${WORKMATE_SERVICE_TOKEN}"
      APP_BASE_URL: "http://workmate-agent:8001/a2a"
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - "import urllib.request; urllib.request.urlopen('http://localhost:8001/health/ready')"
      interval: 5s
      timeout: 3s
      retries: 10
      start_period: 5s
```

오케스트레이터 저장소의 `.env`에는 두 서비스가 공유할 같은 Token을 설정합니다. `.env`는 GitHub에 올리지 않습니다.

```env
WORKMATE_SERVICE_TOKEN=팀에서-합의한-테스트-토큰
```

통합 Compose 실행:

```powershell
docker compose up --build -d
docker compose ps
docker compose logs -f orchestrator workmate-agent
```

Compose가 만드는 공통 Network 안에서는 `workmate-agent`가 DNS 호스트명으로 동작합니다. 오케스트레이터 컨테이너에서 `localhost:8001`을 사용하면 안 됩니다.

## A2A 호출 예시

요청:

```http
POST http://workmate-agent:8001/a2a/message:send
Authorization: Bearer 팀에서-합의한-테스트-토큰
A2A-Version: 1.0
Content-Type: application/a2a+json
```

```json
{
  "message": {
    "messageId": "msg-test-001",
    "role": "ROLE_USER",
    "parts": [
      {
        "data": {
          "skill_id": "daily_briefing",
          "user_id": "user-123",
          "timezone": "Asia/Seoul",
          "as_of": "2026-08-05T09:00:00+09:00",
          "locale": "ko-KR"
        },
        "mediaType": "application/json"
      }
    ]
  },
  "configuration": {
    "acceptedOutputModes": ["application/json", "text/markdown"]
  },
  "metadata": {
    "request_id": "req-test-001"
  }
}
```

응답의 확인 대상:

```text
task.status.state = TASK_STATE_COMPLETED
task.artifacts[].parts[].mediaType = text/markdown 또는 application/json
result.type = daily_briefing
result.mock = true
```

## 문제 확인

```powershell
docker compose ps
docker compose logs workmate-agent
```

| 응답 | 원인 |
| --- | --- |
| `400 A2A-Version must be 1.0` | `A2A-Version` Header 누락 또는 불일치 |
| `400 Unknown skill_id` | 지원하지 않는 Skill 요청 |
| `401 Invalid service token` | Bearer Token 누락 또는 불일치 |
| `404 Task not found` | 존재하지 않거나 재시작으로 사라진 Mock Task |
| `503 Service token is not configured` | Agent 컨테이너에 환경변수 미설정 |

Mock Task는 메모리에만 저장되므로 컨테이너를 재시작하면 사라집니다.

## 통신 테스트

컨테이너가 실행 중인 상태에서 저장소 폴더의 테스트를 실행합니다.

```powershell
$env:WORKMATE_SERVICE_TOKEN='팀에서-합의한-테스트-토큰'
python smoke_test.py
```

다른 Host나 Port를 검사할 때만 URL을 변경합니다.

```powershell
$env:WORKMATE_TEST_BASE_URL='http://localhost:8001'
python smoke_test.py
```

성공 출력:

```text
PASS: Workmate A2A smoke test (http://localhost:8001, 5 skills)
```
