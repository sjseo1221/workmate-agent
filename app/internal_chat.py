"""M0.3-01 내부 Skill 입력 검증 API.

이 모듈은 검증 챗봇의 입력 경계만 제공한다. 실제 Workflow 호출, 장기
Task 조회, Session/OIDC 인증은 후속 M0.3 백로그에서 이 경계에 연결한다.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.a2a.runtime import approved_skill_definitions, workflow_registry
from app.workflows.registry import WorkflowRequest


INTERNAL_CHAT_ENABLED_ENV = "WORKMATE_INTERNAL_CHAT_ENABLED"


def _enabled() -> bool:
    """내부 검증 API 활성화 여부를 환경변수에서 읽는다."""

    return os.getenv(INTERNAL_CHAT_ENABLED_ENV, "").lower() == "true"


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


router = APIRouter(prefix="/api/v1/internal/skill-chat", tags=["internal-skill-chat"])


@router.get("/skills")
def list_skills() -> list[dict[str, str]]:
    """Agent Card와 동일한 승인 Skill 5개를 반환한다."""

    if not _enabled():
        raise HTTPException(status_code=404, detail="internal chat is disabled")
    return list(approved_skill_definitions())


@router.post("/messages", response_model=SkillChatMessageResponse)
async def validate_message(request: SkillChatMessageRequest) -> SkillChatMessageResponse:
    """입력을 검증하고 A2A와 공유하는 Workflow Registry를 호출한다."""

    if not _enabled():
        raise HTTPException(status_code=404, detail="internal chat is disabled")
    known_skills = {skill["id"] for skill in approved_skill_definitions()}
    if request.skill_id not in known_skills:
        raise HTTPException(status_code=422, detail="unknown skill_id")
    input_type = "natural_language" if isinstance(request.input, str) else "json"
    payload = {"text": request.input} if isinstance(request.input, str) else request.input
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


__all__ = [
    "INTERNAL_CHAT_ENABLED_ENV",
    "SkillChatMessageRequest",
    "SkillChatMessageResponse",
    "router",
]
