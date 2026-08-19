"""Official A2A SDK runtime bootstrap for Workmate.

This module owns the transport runtime only. Business workflows and their
approved Artifact contracts are added in later M0/M1 work.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import datetime, timezone
from uuid import uuid4

from google.protobuf import json_format
from google.protobuf.timestamp_pb2 import Timestamp
from a2a.server.agent_execution import AgentExecutor
from a2a.server.events import EventQueue, InMemoryQueueManager
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_rest_routes
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    Artifact,
    HTTPAuthSecurityScheme,
    Part,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
)
from starlette.routing import BaseRoute

from app.a2a.persistence import PostgresTaskStore, PersistentTaskStore
from app.workflows.registry import WorkflowRegistry, WorkflowRequest, WorkflowResult
from app.workflows.priority import build_rank_priorities_workflow
from app.workflows.weekly_report import build_weekly_report_workflow
from app.workflows.daily_briefing import build_daily_briefing_workflow
from app.workflows.meetings import (
    build_analyze_meeting_workflow,
    build_search_meetings_workflow,
)
from app.workflows.assistant_skills import (
    get_meeting_analysis_workflow,
    manage_tasks_workflow,
    read_email_workflow,
    review_action_items_workflow,
    review_proposal_workflow,
)
from app.workflows.assistant_router import assistant_ask_workflow


A2A_VERSION = "1.0"
A2A_PATH_PREFIX = "/a2a"
SERVICE_TOKEN_ENV = "WORKMATE_SERVICE_TOKEN"
TASK_DB_PATH_ENV = "WORKMATE_TASK_DB_PATH"
DATABASE_URL_ENV = "DATABASE_URL"
BASE_URL = os.getenv(
    "APP_BASE_URL", "http://workmate-agent:8001/a2a"
).rstrip("/")
SERVICE_URL = BASE_URL.removesuffix(A2A_PATH_PREFIX)

_SKILLS = (
    ("daily_briefing", "Daily briefing", "Return the user's daily work briefing."),
    ("weekly_report", "Weekly report", "Return a source-backed weekly report."),
    ("analyze_meeting", "Meeting analysis", "Analyze a meeting transcript."),
    ("search_meetings", "Meeting search", "Search approved meeting transcripts."),
    ("rank_priorities", "Priority ranking", "Calculate the deterministic Top 3."),
    # 2026-08-17, 15번 문서 결정(20번 문서 구현) — 이전엔 Drawer 전용 내부 Skill로만
    # 두고 Orchestrator Agent Card에는 올리지 않았지만(18번 문서 "핵심 설계 결정 1"),
    # 그 결정을 뒤집어 나머지 6개도 여기 합류시켰다. `assistant_ask`(자연어 Router)는
    # 자기 자신을 뺀 나머지 10개 중 무엇을 호출할지 LLM으로 판단하는 Meta Skill이다.
    ("get_meeting_analysis", "Get meeting analysis", "Re-fetch a meeting's stored summary and action items."),
    ("review_action_items", "Review action items", "Batch approve, edit, or reject meeting action item candidates."),
    ("review_proposal", "Review proposal", "Approve or ignore an email/calendar task proposal."),
    ("manage_tasks", "Manage tasks", "List, create, update, or delete the caller's tasks."),
    ("read_email", "Read email", "Fetch an email's live content from Gmail for a grounded answer."),
    ("assistant_ask", "Assistant ask", "Answer a free-text question by routing it to the right skill and phrasing the result in natural language."),
)


def approved_skill_definitions() -> tuple[dict[str, str], ...]:
    """내부 검증 API(Drawer 포함)가 쓸 수 있는 Skill 정의를 반환한다.

    지금은 Agent Card(`_SKILLS`)와 완전히 같은 11개다 — 예전엔 내부 전용 Skill을
    따로 둬서 Agent Card보다 많았지만(2026-08-17 이전), 그 구분이 없어졌다.

    Returns:
        식별자·이름·설명 목록. 반환값은 호출자가 수정할 수 없도록 새 딕셔너리
        튜플로 만든다.
    """

    return tuple(
        {"id": skill_id, "name": name, "description": description}
        for skill_id, name, description in _SKILLS
    )


def build_agent_card() -> AgentCard:
    """Build the SDK Agent Card exposed at the well-known route.

    Returns:
        An SDK ``AgentCard`` declaring the HTTP+JSON 1.0 interface, five
        approved skills, bearer authentication metadata, and Streaming
        capability. The card contains no credential value.
    """

    card = AgentCard(
        name="Workmate AI",
        description="Workmate AI work-management agent.",
        version="0.1.0",
        documentation_url=f"{SERVICE_URL}/docs",
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json", "text/markdown"],
    )
    card.supported_interfaces.add(
        url=BASE_URL,
        protocol_binding="HTTP+JSON",
        protocol_version=A2A_VERSION,
    )
    card.provider.CopyFrom(
        AgentProvider(organization="Workmate", url=SERVICE_URL)
    )
    card.security_schemes["serviceBearer"].http_auth_security_scheme.CopyFrom(
        HTTPAuthSecurityScheme(
            description="Bearer service token for the orchestrator.",
            scheme="Bearer",
            bearer_format="opaque",
        )
    )
    requirement = card.security_requirements.add()
    requirement.schemes["serviceBearer"].SetInParent()
    for skill_id, name, description in _SKILLS:
        card.skills.add(
            id=skill_id,
            name=name,
            description=description,
            tags=["work-management"],
            examples=[],
            input_modes=["application/json"],
            output_modes=["application/json", "text/markdown"],
        )
    return card


# 오케스트레이터의 "담당자" 드롭다운(`AI-agent_game_platform/frontend/src/assignee.ts`) 이름
# 4개를 Workmate user_id로 매핑한다. `tools/m51_legacy_adapter.py`의 같은 이름 상수와 값이
# 같아야 한다 — 그 어댑터는 `app`에 의도적으로 의존하지 않는 독립 실행 도구라 이 모듈을
# import할 수 없어(순환/불필요한 의존 방지) 부득이 값만 중복해 둔다. "서선정"만 실제 Google
# 연동 데이터가 있는 Workmate 계정이고(2026-08-18, `.runtime/tasks.sqlite3` 확인), 나머지
# 3명은 자릿수·형식만 맞춘 placeholder라 매핑돼도 빈 결과만 나온다.
ASSIGNEE_USER_IDS = {
    "서선정": "10464531542706509691",
    "배동우": "267494469329567778120",
    "이승현": "568401699951365934381",
    "변해훈": "898605867716224776814",
}


class RuntimeBootstrapExecutor(AgentExecutor):
    """Deterministic SDK executor used until business workflows are connected."""

    def __init__(
        self, task_store: PersistentTaskStore | PostgresTaskStore, registry: WorkflowRegistry
    ) -> None:
        self.task_store = task_store
        self.registry = registry

    @staticmethod
    def _resolve_user_id(payload: dict[str, object], request_metadata: dict[str, object]) -> str:
        """공개 A2A 요청의 실행 사용자를 정한다.

        `payload["user_id"]`(요청 `data` Part에 명시된 값)가 최우선이다 — 기존
        호출자(계약 테스트 등)가 이미 이 필드를 쓰고 있으므로 그대로 유지한다.
        그게 없을 때만 `request_metadata.owner`(오케스트레이터가 보내는 담당자
        이름, 2026-08-18 R5 — `SendMessageRequest.metadata`이지 `Message.metadata`가
        아니다. 실측으로 확인: `a2a_client.py`가 `{"message": {...}, "metadata": {...}}`를
        평평하게 보내는데, `metadata`는 `message`의 하위 필드가 아니라 요청 자체의 형제
        필드라서 `context.message.metadata`가 아니라 `context.metadata`로 와야 읽힌다)를
        `ASSIGNEE_USER_IDS`로 매핑해 대신 쓴다 — 이것도 없으면 기존과 동일하게
        `"service"`로 폴백한다. 즉 새 조회 경로를 하나 추가할 뿐, 기존에 `user_id`를
        보내던 호출자의 동작은 전혀 바뀌지 않는다.
        """

        explicit_user_id = payload.get("user_id")
        if explicit_user_id:
            return str(explicit_user_id)
        owner = request_metadata.get("owner") if request_metadata else None
        if isinstance(owner, str) and owner in ASSIGNEE_USER_IDS:
            return ASSIGNEE_USER_IDS[owner]
        return "service"

    async def execute(self, context, event_queue: EventQueue) -> None:
        """Publish a transport-only completion for runtime smoke checks.

        Contract:
            The first event is a submitted ``Task``. Status, artifact, and
            completed events follow in that order. This executor deliberately
            emits no business Artifact; Workflow integration owns that work.
        """

        payload = self._payload(context)
        skill_id = str(payload.get("skill_id", "runtime_bootstrap"))
        message = context.message
        message_id = message.message_id if message else f"task-{context.task_id}"
        timestamp = Timestamp()
        timestamp.FromDatetime(datetime.now(timezone.utc))
        submitted = Task(
            id=context.task_id,
            context_id=context.context_id,
            status=TaskStatus(
                state=TaskState.TASK_STATE_SUBMITTED,
                timestamp=timestamp,
            ),
        )
        # Message·Checkpoint·Artifact가 FK로 Task Snapshot을 참조하므로
        # SDK EventQueue보다 먼저 최소 Task를 영속화한다.
        await self.task_store.save(submitted, context.call_context)
        await self.task_store.record_message(
            message_id=message_id,
            task_id=context.task_id,
            payload=payload,
        )
        await self.task_store.save_checkpoint(
            thread_id=context.task_id,
            checkpoint_id=f"{context.task_id}:submitted",
            state={"skill_id": skill_id, "task_id": context.task_id},
            metadata={"message_id": message_id, "phase": "submitted"},
        )
        await event_queue.enqueue_event(submitted)
        result = await self.registry.execute(
            WorkflowRequest(
                skill_id=skill_id,
                task_id=context.task_id,
                thread_id=context.task_id,
                message_id=message_id,
                user_id=self._resolve_user_id(payload, context.metadata),
                payload=payload,
            )
        )
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()
        artifact = Artifact(
            artifact_id=str(uuid4()),
            name=result.artifact_name,
            description=result.artifact_description,
            parts=_artifact_parts(result),
        )
        await self.task_store.save_artifact(context.task_id, artifact)
        await event_queue.enqueue_event(
            TaskArtifactUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                artifact=artifact,
                last_chunk=True,
            )
        )
        await updater.complete()

    @staticmethod
    def _payload(context) -> dict[str, object]:
        """SDK Message의 JSON Part를 Workflow 입력 객체로 변환한다."""

        message = context.message
        if not message:
            return {}
        for part in message.parts:
            if part.HasField("data"):
                return dict(json_format.MessageToDict(part.data))
        return {"text": context.get_user_input()}

    async def cancel(self, context, event_queue: EventQueue) -> None:
        """Publish cancellation through the SDK TaskUpdater.

        Contract:
            The task is moved to a terminal cancelled state through the SDK
            event queue, so callers can observe cancellation consistently.
        """

        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await self.task_store.mark_cancel_requested(context.task_id)
        await updater.cancel()


def _artifact_parts(result: WorkflowResult) -> list[Part]:
    """Workflow 결과를 승인된 A2A Artifact Part로 변환한다.

    구조화 결과가 있으면 JSON DataPart를 먼저 만들고 Markdown 표현을 뒤에
    추가한다. `text`만 읽는 Client(레거시 Orchestrator 포함)도 결과를 볼 수
    있도록, Markdown이 없는 구조화 결과에는 `result.text`를 text/plain Part로
    함께 제공한다. 구조화 결과가 전혀 없는 기존 Workflow는 기존 text/plain
    계약을 그대로 유지한다.
    """

    parts: list[Part] = []
    if result.data is not None:
        data_part = Part(media_type="application/json")
        json_format.ParseDict(result.data, data_part.data)
        parts.append(data_part)
    if result.markdown is not None:
        parts.append(Part(text=result.markdown, media_type="text/markdown"))
    elif result.data is not None:
        parts.append(Part(text=result.text, media_type="text/plain"))
    if not parts:
        parts.append(Part(text=result.text, media_type="text/plain"))
    return parts


def _allowed_rest_routes(routes: Iterable[BaseRoute]) -> list[BaseRoute]:
    """Keep only the public MVP REST operations from the SDK route factory.

    Args:
        routes: Routes returned by the official SDK factory.

    Returns:
        The allowlisted send, stream, task, cancel, and subscribe routes.
        Push notification, task-list, extended-card, and tenant mounts are
        intentionally excluded from the public surface.
    """

    allowed = {
        (f"{A2A_PATH_PREFIX}/message:send", frozenset({"POST"})),
        (f"{A2A_PATH_PREFIX}/message:stream", frozenset({"POST"})),
        (f"{A2A_PATH_PREFIX}/tasks/{{id}}", frozenset({"GET", "HEAD"})),
        (f"{A2A_PATH_PREFIX}/tasks/{{id}}:cancel", frozenset({"POST"})),
        (f"{A2A_PATH_PREFIX}/tasks/{{id}}:subscribe", frozenset({"POST"})),
    }
    return [
        route
        for route in routes
        if (getattr(route, "path", None), frozenset(getattr(route, "methods", set())))
        in allowed
    ]


def build_runtime_routes() -> list[BaseRoute]:
    """Create the Agent Card and allowlisted SDK REST routes.

    Returns:
        A route list containing the public Agent Card route and the approved
        SDK REST operations. The handler uses the configured SQLite or
        PostgreSQL Task Store for durable Task snapshots and idempotency.
    """

    card = build_agent_card()
    global _TASK_STORE
    _TASK_STORE = build_task_store()
    task_store = _TASK_STORE
    global _WORKFLOW_REGISTRY
    registry = WorkflowRegistry()

    async def runtime_workflow(request: WorkflowRequest) -> WorkflowResult:
        """Runtime 골격을 확인하는 비즈니스 독립 Workflow."""

        return WorkflowResult(
            artifact_name="runtime_bootstrap",
            artifact_description="Runtime readiness artifact; no business result.",
            text="Workmate A2A runtime is ready for workflow integration.",
            mock=True,
        )

    registry.register("rank_priorities", build_rank_priorities_workflow())
    registry.register("daily_briefing", build_daily_briefing_workflow())
    registry.register("weekly_report", build_weekly_report_workflow)
    registry.register("analyze_meeting", build_analyze_meeting_workflow())
    registry.register("search_meetings", build_search_meetings_workflow())
    registry.register("runtime_bootstrap", runtime_workflow)
    # Workmate 어시스턴트(Drawer)가 쓰려고 추가한 Skill이지만, 지금은 위 5개와
    # 마찬가지로 Agent Card에 공개돼 있어 공개 A2A로도 호출할 수 있다(2026-08-17,
    # 15번 문서 결정). Drawer(내부 챗봇 경로)도 같은 Registry를 그대로 호출한다.
    registry.register("get_meeting_analysis", get_meeting_analysis_workflow)
    registry.register("review_action_items", review_action_items_workflow)
    registry.register("review_proposal", review_proposal_workflow)
    registry.register("manage_tasks", manage_tasks_workflow)
    registry.register("read_email", read_email_workflow)
    # 자연어 Router — Drawer의 Skill 드롭다운을 대체하고, 오케스트레이터 어댑터가
    # Skill 판별을 대신 맡기는 대상이기도 하다(20번 문서). `assistant_ask_workflow`
    # 안에서 이 Registry를 다시 호출해(지연 Import) 실제 Skill을 실행한다.
    registry.register("assistant_ask", assistant_ask_workflow)
    _WORKFLOW_REGISTRY = registry

    handler = DefaultRequestHandler(
        agent_executor=RuntimeBootstrapExecutor(task_store, registry),
        task_store=task_store,
        agent_card=card,
        queue_manager=InMemoryQueueManager(),
    )
    return [
        *create_agent_card_routes(card),
        *_allowed_rest_routes(create_rest_routes(handler, path_prefix=A2A_PATH_PREFIX)),
    ]


_TASK_STORE: PersistentTaskStore | PostgresTaskStore | None = None
_WORKFLOW_REGISTRY: WorkflowRegistry | None = None


def build_task_store() -> PersistentTaskStore | PostgresTaskStore:
    """환경에 맞는 영속 Task Store를 만든다.

    `DATABASE_URL`이 있으면 PostgreSQL을 사용하고, 없으면 개발·Contract
    Test용 SQLite 파일을 사용한다. 운영 Compose는 반드시 `DATABASE_URL`을
    주입해야 한다.
    """

    database_url = os.getenv(DATABASE_URL_ENV)
    if database_url:
        return PostgresTaskStore(database_url)
    return PersistentTaskStore(os.getenv(TASK_DB_PATH_ENV, ".runtime/a2a.sqlite3"))


def task_store() -> PersistentTaskStore | PostgresTaskStore:
    """현재 Runtime이 사용하는 영속 Task Store를 반환한다."""

    if _TASK_STORE is None:
        raise RuntimeError("runtime task store has not been initialized")
    return _TASK_STORE


def workflow_registry() -> WorkflowRegistry:
    """현재 A2A와 내부 검증 API가 공유하는 Workflow Registry를 반환한다."""

    if _WORKFLOW_REGISTRY is None:
        raise RuntimeError("workflow registry has not been initialized")
    return _WORKFLOW_REGISTRY


def is_a2a_path(path: str) -> bool:
    """Return whether a request targets the protected A2A route space.

    Args:
        path: ASGI request path.

    Returns:
        ``True`` for ``/a2a`` and its descendants; ``False`` for health and
        Agent Card routes.
    """

    return path == A2A_PATH_PREFIX or path.startswith(f"{A2A_PATH_PREFIX}/")


def service_token() -> str:
    """Read the service token at request time.

    Returns:
        The configured opaque token, or an empty string when configuration is
        missing. The value is never included in responses or documentation.
    """

    return os.getenv(SERVICE_TOKEN_ENV, "")


__all__ = [
    "A2A_PATH_PREFIX",
    "A2A_VERSION",
    "BASE_URL",
    "SERVICE_TOKEN_ENV",
    "TASK_DB_PATH_ENV",
    "DATABASE_URL_ENV",
    "build_task_store",
    "build_agent_card",
    "build_runtime_routes",
    "is_a2a_path",
    "service_token",
    "workflow_registry",
]
