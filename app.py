import hmac
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


A2A_VERSION = "1.0"
BASE_URL = os.getenv(
    "APP_BASE_URL", "http://workmate-agent:8001/a2a"
).rstrip("/")
SERVICE_URL = BASE_URL.removesuffix("/a2a")
SERVICE_TOKEN = os.getenv("WORKMATE_SERVICE_TOKEN", "")

SKILLS = {
    "daily_briefing": ("오늘 업무 브리핑", "오늘의 업무와 우선순위 Top 3를 반환합니다."),
    "weekly_report": ("주간 업무보고", "이번 주 업무보고를 반환합니다."),
    "analyze_meeting": ("회의 분석", "회의 요약과 Action Item 후보를 반환합니다."),
    "search_meetings": ("이전 회의록 검색", "회의록 검색 결과와 출처를 반환합니다."),
    "rank_priorities": ("업무 우선순위 계산", "업무 우선순위 Top 3를 반환합니다."),
}

TASKS: dict[str, dict[str, Any]] = {}


class MessagePart(BaseModel):
    data: dict[str, Any]
    mediaType: str = "application/json"


class Message(BaseModel):
    messageId: str
    role: str
    parts: list[MessagePart]


class SendMessageRequest(BaseModel):
    message: Message
    configuration: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


app = FastAPI(title="Workmate AI Agent Shell", version="0.1.0")


def verify_a2a_headers(
    authorization: str | None = Header(default=None),
    a2a_version: str | None = Header(default=None, alias="A2A-Version"),
) -> None:
    if not SERVICE_TOKEN:
        raise HTTPException(status_code=503, detail="Service token is not configured")
    expected = f"Bearer {SERVICE_TOKEN}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Invalid service token")
    if a2a_version != A2A_VERSION:
        raise HTTPException(status_code=400, detail="A2A-Version must be 1.0")


@app.get("/.well-known/agent-card.json")
def get_agent_card() -> dict[str, Any]:
    return {
        "name": "Workmate AI",
        "description": "A2A 통신 검증용 Workmate AI Mock Agent",
        "supportedInterfaces": [
            {
                "url": BASE_URL,
                "protocolBinding": "HTTP+JSON",
                "protocolVersion": A2A_VERSION,
            }
        ],
        "provider": {"organization": "Game Team", "url": SERVICE_URL},
        "version": "0.1.0",
        "documentationUrl": f"{SERVICE_URL}/docs",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extendedAgentCard": False,
        },
        "securitySchemes": {
            "serviceBearer": {
                "httpAuthSecurityScheme": {
                    "description": "Orchestrator와 Workmate 사이의 Service Token",
                    "scheme": "Bearer",
                    "bearerFormat": "opaque",
                }
            }
        },
        "securityRequirements": [
            {"schemes": {"serviceBearer": {"list": []}}}
        ],
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json", "text/markdown"],
        "skills": [
            {
                "id": skill_id,
                "name": name,
                "description": description,
                "tags": ["mock"],
                "examples": [],
                "inputModes": ["application/json"],
                "outputModes": ["application/json", "text/markdown"],
            }
            for skill_id, (name, description) in SKILLS.items()
        ],
    }


@app.post("/a2a/message:send")
def send_message(
    envelope: SendMessageRequest,
    _: None = Depends(verify_a2a_headers),
) -> dict[str, Any]:
    if not envelope.message.parts:
        raise HTTPException(status_code=400, detail="Message parts are required")

    data = envelope.message.parts[0].data
    skill_id = data.get("skill_id")
    if skill_id not in SKILLS:
        raise HTTPException(status_code=400, detail=f"Unknown skill_id: {skill_id}")

    task_id = f"task-{uuid4()}"
    context_id = f"context-{uuid4()}"
    now = datetime.now(timezone.utc).isoformat()
    name, description = SKILLS[skill_id]
    task = {
        "id": task_id,
        "contextId": context_id,
        "status": {"state": "TASK_STATE_COMPLETED", "timestamp": now},
        "artifacts": [
            {
                "artifactId": f"artifact-{uuid4()}",
                "name": name,
                "description": f"{name} Mock 결과",
                "parts": [
                    {
                        "text": f"# {name}\n\n{description}\n\nMock 호출에 성공했습니다.",
                        "mediaType": "text/markdown",
                    },
                    {
                        "data": {
                            "schema_version": "1.0",
                            "request_id": envelope.metadata.get("request_id"),
                            "generated_at": now,
                            "result": {
                                "type": skill_id,
                                "mock": True,
                                "received": data,
                            },
                        },
                        "mediaType": "application/json",
                    },
                ],
            }
        ],
    }
    TASKS[task_id] = task
    return {"task": task}


@app.get("/a2a/tasks/{task_id}")
def get_task(
    task_id: str,
    _: None = Depends(verify_a2a_headers),
) -> dict[str, Any]:
    task = TASKS.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": task}


@app.post("/a2a/tasks/{task_id}:cancel")
def cancel_task(
    task_id: str,
    _: None = Depends(verify_a2a_headers),
) -> dict[str, Any]:
    task = TASKS.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"]["state"] == "TASK_STATE_COMPLETED":
        raise HTTPException(status_code=409, detail="Completed Task cannot be cancelled")

    now = datetime.now(timezone.utc).isoformat()
    task["status"] = {"state": "TASK_STATE_CANCELED", "timestamp": now}
    return {"task": task}


@app.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/health/ready")
def health_ready() -> dict[str, str]:
    if not SERVICE_TOKEN:
        raise HTTPException(status_code=503, detail="Service token is not configured")
    return {"status": "ready"}
