"""A2A 입력을 업무 Workflow 선택으로 변환하는 경계.

이 모듈은 Route나 SDK 타입을 알지 않으며, `skill_id`별 Workflow 등록과
호출만 담당한다. 실제 Gmail·Calendar·Task 업무 규칙은 후속 마일스톤의
Workflow가 구현하고, 이 경계는 해당 결과를 A2A Artifact로 변환할 수 있는
작은 공통 결과를 반환한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable


class WorkflowNotImplementedError(RuntimeError):
    """아직 구현되지 않은 업무 Workflow를 호출했을 때 발생한다."""


@dataclass(frozen=True, slots=True)
class WorkflowRequest:
    """Workflow에 전달하는 전송 독립 요청.

    Args:
        skill_id: 공개 Skill 식별자.
        task_id: A2A 장기 Task 식별자.
        thread_id: Checkpoint 재개에 사용하는 LangGraph thread 식별자.
        message_id: A2A Message 멱등성 식별자.
        user_id: 사용자 권한 범위 식별자.
        payload: 승인된 Skill 입력 JSON.
    """

    skill_id: str
    task_id: str
    thread_id: str
    message_id: str
    user_id: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class WorkflowResult:
    """Workflow 실행 결과의 공통 표현."""

    artifact_name: str
    artifact_description: str
    text: str
    state: str = "completed"
    warnings: list[dict[str, object]] = field(default_factory=list)
    mock: bool = False


WorkflowHandler = Callable[[WorkflowRequest], Awaitable[WorkflowResult]]


class WorkflowRegistry:
    """공개 `skill_id`를 등록된 Workflow 핸들러에 연결한다."""

    def __init__(self) -> None:
        self._handlers: dict[str, WorkflowHandler] = {}

    def register(self, skill_id: str, handler: WorkflowHandler) -> None:
        """Skill 핸들러를 등록한다.

        Args:
            skill_id: Agent Card에 공개한 Skill 식별자.
            handler: 전송 독립 요청을 처리하는 비동기 함수.
        Raises:
            ValueError: 빈 Skill ID이거나 이미 등록된 경우.
        """

        if not skill_id:
            raise ValueError("skill_id must not be empty")
        if skill_id in self._handlers:
            raise ValueError(f"workflow already registered: {skill_id}")
        self._handlers[skill_id] = handler

    def resolve(self, skill_id: str) -> WorkflowHandler:
        """Skill ID에 대응하는 Workflow를 반환한다."""

        try:
            return self._handlers[skill_id]
        except KeyError as exc:
            raise WorkflowNotImplementedError(
                f"workflow is not implemented: {skill_id}"
            ) from exc

    async def execute(self, request: WorkflowRequest) -> WorkflowResult:
        """Registry를 통해 Workflow를 실행한다."""

        return await self.resolve(request.skill_id)(request)
