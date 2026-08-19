"""Gmail·Calendar 임시 Task 제안과 사용자 승인 경계.

제안 본문(제목·스니펫·LLM 근거)은 DB에 저장하지 않고 사용자별 메모리 Hub와
SSE로만 전달한다. 사용자가 승인한 순간에만 기존 Task Repository에 원본
중복·유사 중복 검사를 거쳐 Task를 저장한다. "무시"는 Task를 만들지 않지만,
같은 후보가 재동기화 때마다 다시 노출되지 않도록 `IgnoredProposalRepository`에
ID·시각만(본문 없이) 영구 기록한다(17번 갭 문서 #2, 2026-08-16).
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import lru_cache
import json
import os
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.domain.task import TaskRecord
from app.internal_chat import _authenticated_user_or_assignee
from app.providers.google import CalendarEvent, GmailMessage
from app.repositories.ignored_proposals import (
    IgnoredProposalRepository,
    PostgresIgnoredProposalRepository,
    SQLiteIgnoredProposalRepository,
)
from app.task_api import DATABASE_URL_ENV, _task_response, task_repository


ProposalSourceType = Literal["email", "calendar"]
ProposalDecision = Literal["approve", "ignore"]

IGNORED_PROPOSALS_DB_PATH_ENV = "WORKMATE_IGNORED_PROPOSALS_DB_PATH"


@lru_cache(maxsize=1)
def ignored_proposal_repository() -> IgnoredProposalRepository:
    """환경 설정에 맞는 단일 "무시" 기록 Repository를 반환한다(`task_repository()`와 동일한 선택 규칙)."""

    database_url = os.getenv(DATABASE_URL_ENV)
    if database_url:
        return PostgresIgnoredProposalRepository(database_url)
    return SQLiteIgnoredProposalRepository(os.getenv(IGNORED_PROPOSALS_DB_PATH_ENV, ".runtime/ignored_proposals.sqlite3"))


@dataclass(frozen=True, slots=True)
class TaskProposal:
    """SSE로 전달할 사용자별 임시 Task 제안이다."""

    proposal_id: str
    user_id: str
    source_type: ProposalSourceType
    source_id: str
    title: str
    assignee_user_id: str
    due_at: datetime | None = None
    priority_hint: int | None = None
    metadata: dict[str, Any] | None = None

    def as_payload(self) -> dict[str, Any]:
        """SSE와 검토 응답에 사용할 비식별 JSON을 만든다."""

        payload = asdict(self)
        if self.due_at is not None:
            payload["due_at"] = self.due_at.isoformat()
        return payload


class ProposalTaskInput(BaseModel):
    """사용자가 승인 전에 수정할 Task 필드다."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    assignee_user_id: str = Field(min_length=1, max_length=200)
    due_at: datetime | None = None
    priority_hint: int | None = Field(default=None, ge=0, le=10)


class EmailProposalReviewRequest(BaseModel):
    """메일 제안 승인 또는 무시 요청이다."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, max_length=500)
    decision: ProposalDecision
    task: ProposalTaskInput | None = None
    allow_similar_duplicate: bool = False


class CalendarProposalReviewRequest(BaseModel):
    """Calendar 제안 승인 또는 무시 요청이다."""

    model_config = ConfigDict(extra="forbid")

    calendar_id: str = Field(min_length=1, max_length=500)
    event_id: str = Field(min_length=1, max_length=500)
    decision: ProposalDecision
    task: ProposalTaskInput | None = None
    allow_similar_duplicate: bool = False


class ProposalHub:
    """DB에 보존하지 않는 사용자별 제안·SSE 큐다."""

    def __init__(self) -> None:
        self._proposals: dict[tuple[str, ProposalSourceType, str], TaskProposal] = {}
        self._queues: dict[str, asyncio.Queue[TaskProposal]] = {}
        self._idempotency: dict[tuple[str, str], dict[str, Any]] = {}

    @staticmethod
    def _key(proposal: TaskProposal) -> tuple[str, ProposalSourceType, str]:
        return proposal.user_id, proposal.source_type, proposal.source_id

    async def publish(self, proposal: TaskProposal) -> TaskProposal:
        """제안을 사용자 큐에 넣고 동일 원본은 최신 제안으로 교체한다."""

        self._proposals[self._key(proposal)] = proposal
        queue = self._queues.setdefault(proposal.user_id, asyncio.Queue())
        await queue.put(proposal)
        return proposal

    def get(self, user_id: str, source_type: ProposalSourceType, source_id: str) -> TaskProposal | None:
        """요청 사용자와 원본이 일치하는 제안만 반환한다."""

        return self._proposals.get((user_id, source_type, source_id))

    def discard(self, proposal: TaskProposal) -> None:
        """검토가 끝난 제안을 메모리에서 제거한다."""

        self._proposals.pop(self._key(proposal), None)

    def recent(self, user_id: str, *, limit: int = 10) -> list[TaskProposal]:
        """사용자의 검토 대기 제안을 최근 발행 순으로 최대 `limit`건 반환한다.

        아직 승인·무시되지 않은 제안은 `publish()`가 호출될 때마다 이 메모리
        (`self._proposals`)에 그대로 남아 있다 — 별도 저장소가 필요 없다.
        `dict`는 삽입 순서를 보존하므로(같은 원본이 재동기화로 다시 발행돼도
        키가 같으면 자리는 그대로 갱신될 뿐이라 완벽한 시각 순서는 아니지만),
        Context 매칭용으로는 충분한 근사치다(2026-08-17, 20번 문서 4단계 —
        `assistant_ask`가 호출자로부터 Context를 못 받을 때 이 목록으로
        대신 채운다)."""

        matches = [proposal for (uid, _, _), proposal in self._proposals.items() if uid == user_id]
        return list(reversed(matches))[:limit]

    def cached_response(self, user_id: str, idempotency_key: str) -> dict[str, Any] | None:
        """동일 승인 요청의 이전 응답을 반환한다."""

        return self._idempotency.get((user_id, idempotency_key))

    def cache_response(self, user_id: str, idempotency_key: str, response: dict[str, Any]) -> None:
        """승인 요청 응답을 프로세스 수명 동안 멱등 캐시한다."""

        self._idempotency[(user_id, idempotency_key)] = response

    def clear(self) -> None:
        """테스트가 사용자별 제안과 멱등 상태를 격리하도록 초기화한다."""

        self._proposals.clear()
        self._queues.clear()
        self._idempotency.clear()

    async def events(self, user_id: str):
        """사용자 제안을 SSE Event 형식으로 무기한 전달한다."""

        queue = self._queues.setdefault(user_id, asyncio.Queue())
        while True:
            proposal = await queue.get()
            if self.get(user_id, proposal.source_type, proposal.source_id) is None:
                continue
            yield f"event: task_proposal\ndata: {json.dumps(proposal.as_payload(), ensure_ascii=False)}\n\n"


proposal_hub = ProposalHub()


async def publish_email_proposal(
    user_id: str,
    message: GmailMessage,
    *,
    title: str,
    due_at: datetime | None = None,
    priority_hint: int | None = None,
    source_id: str | None = None,
    reason: str | None = None,
) -> TaskProposal:
    """정규화된 Gmail Message를 승인 대기 제안으로 발행한다.

    한 메일에서 LLM이 할 일 후보를 여러 개 뽑으면(`email_action_items.py`)
    이 함수를 후보 수만큼 호출한다. `source_id`를 지정하지 않으면 기존처럼
    `message.message_id` 하나로 메일당 제안 하나를 만들고, 지정하면(예:
    `f"{message_id}:0"`) 같은 메일에서 나온 각 후보를 서로 다른 제안으로
    독립적으로 승인·무시할 수 있다.
    """

    resolved_source_id = source_id or message.message_id
    metadata: dict[str, Any] = {"thread_id": message.thread_id, "snippet": message.snippet}
    if reason:
        metadata["reason"] = reason
    if message.received_at:
        # 카드에 수신 날짜를 보여주고 수신일 내림차순으로 정렬하는 데 쓴다
        # (2026-08-17 요청). `due_at`(마감일 개념)과는 다른 값이라 재사용하지
        # 않고 별도 필드로 싣는다 — email 제안의 `due_at`은 항상 `null`이다.
        metadata["received_at"] = message.received_at
    return await proposal_hub.publish(
        TaskProposal(
            proposal_id=f"email-{resolved_source_id}",
            user_id=user_id,
            source_type="email",
            source_id=resolved_source_id,
            title=title,
            assignee_user_id=user_id,
            due_at=due_at,
            priority_hint=priority_hint,
            metadata=metadata,
        )
    )


async def publish_calendar_proposal(
    user_id: str,
    event: CalendarEvent,
    *,
    title: str | None = None,
    due_at: datetime | None = None,
    priority_hint: int | None = None,
) -> TaskProposal:
    """정규화된 Calendar Event를 승인 대기 제안으로 발행한다."""

    return await proposal_hub.publish(
        TaskProposal(
            proposal_id=f"calendar-{event.source_id}",
            user_id=user_id,
            source_type="calendar",
            source_id=event.source_id,
            title=title or event.summary,
            assignee_user_id=user_id,
            due_at=due_at,
            priority_hint=priority_hint,
            metadata={"calendar_id": event.calendar_id, "event_id": event.event_id, "html_link": event.html_link},
        )
    )


def _normalize_title(value: str) -> str:
    """유사 Task 비교용 제목을 공백·대소문자 기준으로 정규화한다."""

    return re.sub(r"\s+", " ", value).strip().casefold()


def _similar_tasks(user_id: str, task: ProposalTaskInput):
    """같은 사용자·제목·마감일의 활성 Task를 찾는다."""

    normalized = _normalize_title(task.title)
    return [
        current
        for current in task_repository().list(user_id)
        if current.assignee_user_id == task.assignee_user_id
        and _normalize_title(current.title) == normalized
        and current.due_at == task.due_at
    ]


def _review(
    *,
    user_id: str,
    source_type: ProposalSourceType,
    source_id: str,
    decision: ProposalDecision,
    task_input: ProposalTaskInput | None,
    allow_similar_duplicate: bool,
    idempotency_key: str,
) -> dict[str, Any]:
    """메일·Calendar 제안의 승인 규칙을 하나의 함수로 적용한다."""

    cached = proposal_hub.cached_response(user_id, idempotency_key)
    if cached is not None:
        return cached
    proposal = proposal_hub.get(user_id, source_type, source_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="task proposal not found")
    if decision == "ignore":
        proposal_hub.discard(proposal)
        ignored_proposal_repository().mark(user_id, source_type, source_id)
        response = {"decision": "ignore", "source_type": source_type, "source_id": source_id, "task": None}
        proposal_hub.cache_response(user_id, idempotency_key, response)
        return response
    if task_input is None:
        raise HTTPException(status_code=422, detail="task is required when decision is approve")
    if task_input.assignee_user_id != user_id:
        raise HTTPException(status_code=403, detail="assignee_user_id must match OIDC subject")
    source_existing = next(
        (item for item in task_repository().list(user_id) if item.source_type == source_type and item.source_id == source_id),
        None,
    )
    if source_existing is None:
        similar = _similar_tasks(user_id, task_input)
        if similar and not allow_similar_duplicate:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "SIMILAR_TASK_EXISTS",
                    "tasks": [_task_response(item).model_dump(mode="json") for item in similar],
                },
            )
    task = source_existing or task_repository().create(
        TaskRecord(
            task_id=f"proposal-{source_type}-{source_id}",
            assignee_user_id=user_id,
            title=task_input.title,
            due_at=task_input.due_at,
            priority_hint=task_input.priority_hint,
            source_type=source_type,
            source_id=source_id,
        )
    )
    proposal_hub.discard(proposal)
    response = {
        "decision": "approve",
        "source_type": source_type,
        "source_id": source_id,
        "created": source_existing is None,
        "task": _task_response(task).model_dump(mode="json"),
    }
    proposal_hub.cache_response(user_id, idempotency_key, response)
    return response


router = APIRouter(prefix="/api/v1", tags=["task-proposals"])


@router.get("/notifications/stream")
async def notifications_stream(user_id: str = Depends(_authenticated_user_or_assignee)) -> StreamingResponse:
    """인증된 사용자에게 메일·Calendar 제안을 SSE로 전달한다."""

    return StreamingResponse(
        proposal_hub.events(user_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/email-task-proposals:review")
def review_email_proposal(
    request: EmailProposalReviewRequest,
    user_id: str = Depends(_authenticated_user_or_assignee),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """메일 제안을 승인·무시하고 승인 시 Task를 멱등 생성한다."""

    if not idempotency_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Idempotency-Key is required")
    return _review(
        user_id=user_id,
        source_type="email",
        source_id=request.message_id,
        decision=request.decision,
        task_input=request.task,
        allow_similar_duplicate=request.allow_similar_duplicate,
        idempotency_key=idempotency_key,
    )


@router.post("/calendar-task-proposals:review")
def review_calendar_proposal(
    request: CalendarProposalReviewRequest,
    user_id: str = Depends(_authenticated_user_or_assignee),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """Calendar 제안을 승인·무시하고 승인 시 Task를 멱등 생성한다."""

    if not idempotency_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Idempotency-Key is required")
    return _review(
        user_id=user_id,
        source_type="calendar",
        source_id=f"{request.calendar_id}:{request.event_id}",
        decision=request.decision,
        task_input=request.task,
        allow_similar_duplicate=request.allow_similar_duplicate,
        idempotency_key=idempotency_key,
    )


__all__ = [
    "CalendarProposalReviewRequest",
    "EmailProposalReviewRequest",
    "ProposalHub",
    "ProposalTaskInput",
    "TaskProposal",
    "publish_calendar_proposal",
    "publish_email_proposal",
    "proposal_hub",
    "router",
]
