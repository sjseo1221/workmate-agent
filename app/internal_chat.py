"""M0.3-01 내부 Skill 입력 검증 API.

이 모듈은 검증 챗봇의 입력 경계만 제공한다. 실제 Workflow 호출, 장기
Task 조회, Session/OIDC 인증은 후속 M0.3 백로그에서 이 경계에 연결한다.
"""

from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from a2a.server.context import ServerCallContext
from a2a.auth.user import User
from a2a.types import Artifact, Part, Task, TaskState, TaskStatus
from google.protobuf import json_format
from jsonschema import Draft202012Validator, FormatChecker
import jwt
from jwt import PyJWKClient
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.a2a.runtime import approved_skill_definitions, task_store, workflow_registry
from app.workflows.registry import WorkflowRequest


INTERNAL_CHAT_ENABLED_ENV = "WORKMATE_INTERNAL_CHAT_ENABLED"
INTERNAL_CHAT_DEV_AUTH_ENV = "WORKMATE_INTERNAL_CHAT_DEV_AUTH"
INTERNAL_CHAT_DEV_USER_ENV = "WORKMATE_INTERNAL_CHAT_DEV_USER_ID"
OIDC_ISSUER_ENV = "WORKMATE_OIDC_ISSUER"
OIDC_AUDIENCE_ENV = "WORKMATE_OIDC_AUDIENCE"
OIDC_JWKS_URL_ENV = "WORKMATE_OIDC_JWKS_URL"
SCHEMA_ROOT_ENV = "WORKMATE_SCHEMA_ROOT"


def _enabled() -> bool:
    """내부 검증 API 활성화 여부를 환경변수에서 읽는다."""

    return os.getenv(INTERNAL_CHAT_ENABLED_ENV, "").lower() == "true"


def _schema_validator() -> Draft202012Validator:
    """승인된 Workmate Skill Schema를 로드한다."""

    configured = os.getenv(SCHEMA_ROOT_ENV)
    root = Path(configured) if configured else Path(__file__).resolve().parents[2] / "docs" / "schemas"
    schema_path = root / "workmate-skill-schemas.schema.json"
    if not schema_path.is_file():
        raise HTTPException(status_code=503, detail="approved skill schema is unavailable")
    return Draft202012Validator(
        json.loads(schema_path.read_text(encoding="utf-8")),
        format_checker=FormatChecker(),
    )


def _authenticated_user(request: Request) -> str:
    """OIDC JWT의 `sub`를 내부 API의 사용자 범위로 반환한다."""

    if os.getenv(INTERNAL_CHAT_DEV_AUTH_ENV, "").lower() == "true":
        user_id = os.getenv(INTERNAL_CHAT_DEV_USER_ENV, "")
        if user_id:
            return user_id
        raise HTTPException(status_code=503, detail="dev auth user is not configured")

    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="OIDC bearer token is required")
    issuer = os.getenv(OIDC_ISSUER_ENV, "")
    audience = os.getenv(OIDC_AUDIENCE_ENV, "")
    jwks_url = os.getenv(OIDC_JWKS_URL_ENV, "")
    if not issuer or not audience or not jwks_url:
        raise HTTPException(status_code=503, detail="OIDC validation is not configured")
    try:
        signing_key = PyJWKClient(jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "sub", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="invalid OIDC bearer token") from exc
    user_id = claims.get("sub")
    if not isinstance(user_id, str) or not user_id:
        raise HTTPException(status_code=401, detail="OIDC subject is missing")
    return user_id


class _InternalUser(User):
    """내부 API에서 영속 Task Store의 소유자 범위를 표현하는 사용자다."""

    def __init__(self, user_id: str) -> None:
        self._user_id = user_id

    @property
    def is_authenticated(self) -> bool:
        """인증된 내부 요청인지 반환한다."""

        return True

    @property
    def user_name(self) -> str:
        """Task Store가 사용할 사용자 식별자를 반환한다."""

        return self._user_id


class SkillChatMessageRequest(BaseModel):
    """선택한 Skill에 전달할 자연어 또는 JSON 입력이다."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str = Field(min_length=1, max_length=100)
    input: str | dict[str, Any]

    @model_validator(mode="after")
    def validate_input(self) -> "SkillChatMessageRequest":
        """빈 입력과 JSON 내부의 다른 Skill 선택을 거부한다."""

        if isinstance(self.input, str) and not self.input.strip():
            raise ValueError("input text must not be blank")
        if isinstance(self.input, dict):
            if not self.input:
                raise ValueError("input JSON must not be empty")
            embedded_skill = self.input.get("skill_id")
            if embedded_skill is not None and embedded_skill != self.skill_id:
                raise ValueError("input.skill_id must match skill_id")
        return self


class SkillChatMessageResponse(BaseModel):
    """입력 검증 결과를 반환한다.

    실제 업무 Artifact의 상세 Schema와 장기 Task 상태는 후속 백로그에서
    보강하며, 이번 응답은 공유 Workflow의 실행 결과를 담는다.
    """

    skill_id: str
    input_type: str
    input: str | dict[str, Any]
    validation: str = "valid"
    task_id: str
    state: str
    artifact: dict[str, Any]
    warnings: list[dict[str, Any]] = Field(default_factory=list)


_TERMINAL_STATES = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
}


async def _persist_task(
    task_id: str,
    request: SkillChatMessageRequest,
    result: Any,
    user_id: str,
) -> Task:
    """Workflow 결과를 기존 A2A Task Store에 Snapshot으로 저장한다."""

    task = Task(
        id=task_id,
        context_id=task_id,
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
    )
    artifact = Artifact(
        artifact_id=f"artifact-{task_id}",
        name=result.artifact_name,
        description=result.artifact_description,
        parts=[Part(text=result.text, media_type="text/plain")],
    )
    task.artifacts.append(artifact)
    context = ServerCallContext(user=_InternalUser(user_id))
    await task_store().save(task, context)
    await task_store().record_message(
        message_id=f"internal-{task_id}",
        task_id=task_id,
        payload={"skill_id": request.skill_id, "input": request.input},
        response=task,
    )
    await task_store().save_artifact(task_id, artifact)
    return task


router = APIRouter(prefix="/api/v1/internal/skill-chat", tags=["internal-skill-chat"])


@router.get("/skills")
def list_skills(user_id: str = Depends(_authenticated_user)) -> list[dict[str, str]]:
    """Agent Card와 동일한 승인 Skill 5개를 반환한다."""

    if not _enabled():
        raise HTTPException(status_code=404, detail="internal chat is disabled")
    return list(approved_skill_definitions())


@router.post("/messages", response_model=SkillChatMessageResponse)
async def validate_message(
    request: SkillChatMessageRequest,
    user_id: str = Depends(_authenticated_user),
) -> SkillChatMessageResponse:
    """입력을 검증하고 A2A와 공유하는 Workflow Registry를 호출한다."""

    if not _enabled():
        raise HTTPException(status_code=404, detail="internal chat is disabled")
    known_skills = {skill["id"] for skill in approved_skill_definitions()}
    if request.skill_id not in known_skills:
        raise HTTPException(status_code=422, detail="unknown skill_id")
    input_type = "natural_language" if isinstance(request.input, str) else "json"
    payload = {"text": request.input} if isinstance(request.input, str) else request.input
    if isinstance(payload, dict):
        payload_user_id = payload.get("user_id")
        if payload_user_id is not None and payload_user_id != user_id:
            raise HTTPException(status_code=403, detail="user_id does not match OIDC subject")
        payload = {**payload, "user_id": user_id}
        if input_type == "json":
            errors = sorted(_schema_validator().iter_errors(payload), key=lambda error: error.path)
            if errors:
                raise HTTPException(status_code=422, detail="input does not match approved skill schema")
    task_id = str(uuid4())
    result = await workflow_registry().execute(
        WorkflowRequest(
            skill_id=request.skill_id,
            task_id=task_id,
            thread_id=task_id,
            message_id=f"internal-{task_id}",
            user_id=str(payload.get("user_id", "internal-chat")),
            payload=payload,
        )
    )
    await _persist_task(task_id, request, result, user_id)
    return SkillChatMessageResponse(
        skill_id=request.skill_id,
        input_type=input_type,
        input=request.input,
        task_id=task_id,
        state=result.state,
        artifact={
            "name": result.artifact_name,
            "description": result.artifact_description,
            "text": result.text,
        },
        warnings=result.warnings,
    )


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, user_id: str = Depends(_authenticated_user)) -> dict[str, Any]:
    """영속 Task Snapshot을 조회해 내부 검증 API의 Polling 결과로 반환한다."""

    task = await task_store().get(task_id, ServerCallContext(user=_InternalUser(user_id)))
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return json_format.MessageToDict(task, preserving_proto_field_name=False)


@router.post("/tasks/{task_id}:cancel")
async def cancel_task(task_id: str, user_id: str = Depends(_authenticated_user)) -> dict[str, Any]:
    """진행 중 Task의 취소를 기록하고 Terminal Task 취소는 거부한다."""

    task = await task_store().get(task_id, ServerCallContext(user=_InternalUser(user_id)))
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status.state in _TERMINAL_STATES:
        raise HTTPException(status_code=409, detail="task is already terminal")
    await task_store().mark_cancel_requested(task_id)
    cancelled = await task_store().get(task_id, ServerCallContext(user=_InternalUser(user_id)))
    return json_format.MessageToDict(cancelled, preserving_proto_field_name=False)


__all__ = [
    "INTERNAL_CHAT_ENABLED_ENV",
    "SkillChatMessageRequest",
    "SkillChatMessageResponse",
    "router",
]
