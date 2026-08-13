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
)


def approved_skill_definitions() -> tuple[dict[str, str], ...]:
    """내부 검증 API가 재사용할 승인된 5개 Skill 정의를 반환한다.

    Returns:
        Agent Card와 내부 검증 API가 공유하는 식별자·이름·설명 목록.
        반환값은 호출자가 수정할 수 없도록 새 딕셔너리 튜플로 만든다.
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


class RuntimeBootstrapExecutor(AgentExecutor):
    """Deterministic SDK executor used until business workflows are connected."""

    def __init__(
        self, task_store: PersistentTaskStore | PostgresTaskStore, registry: WorkflowRegistry
    ) -> None:
        self.task_store = task_store
        self.registry = registry

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
                user_id="service",
                payload=payload,
            )
        )
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()
        artifact = Artifact(
            artifact_id=str(uuid4()),
            name=result.artifact_name,
            description=result.artifact_description,
            parts=[
                Part(
                    text=result.text,
                    media_type="text/plain",
                )
            ],
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

    for skill_id in (skill[0] for skill in _SKILLS):
        registry.register(skill_id, runtime_workflow)
    registry.register("runtime_bootstrap", runtime_workflow)
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
