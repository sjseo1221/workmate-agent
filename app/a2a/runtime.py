"""Official A2A SDK runtime bootstrap for Workmate.

This module owns the transport runtime only. Business workflows and their
approved Artifact contracts are added in later M0/M1 work.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import datetime, timezone

from google.protobuf.timestamp_pb2 import Timestamp
from a2a.server.agent_execution import AgentExecutor
from a2a.server.events import EventQueue, InMemoryQueueManager
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_rest_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    HTTPAuthSecurityScheme,
    Part,
    Task,
    TaskState,
    TaskStatus,
)
from starlette.routing import BaseRoute


A2A_VERSION = "1.0"
A2A_PATH_PREFIX = "/a2a"
SERVICE_TOKEN_ENV = "WORKMATE_SERVICE_TOKEN"
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

    async def execute(self, context, event_queue: EventQueue) -> None:
        """Publish a transport-only completion for runtime smoke checks.

        Contract:
            The first event is a submitted ``Task``. Status, artifact, and
            completed events follow in that order. This executor deliberately
            emits no business Artifact; Workflow integration owns that work.
        """

        timestamp = Timestamp()
        timestamp.FromDatetime(datetime.now(timezone.utc))
        await event_queue.enqueue_event(
            Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_SUBMITTED,
                    timestamp=timestamp,
                ),
            )
        )
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()
        await updater.add_artifact(
            [
                Part(
                    text="Workmate A2A runtime is ready for workflow integration.",
                    media_type="text/plain",
                )
            ],
            name="runtime_bootstrap",
            last_chunk=True,
        )
        await updater.complete()

    async def cancel(self, context, event_queue: EventQueue) -> None:
        """Publish cancellation through the SDK TaskUpdater.

        Contract:
            The task is moved to a terminal cancelled state through the SDK
            event queue, so callers can observe cancellation consistently.
        """

        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
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
        (f"{A2A_PATH_PREFIX}/tasks/{{id}}:subscribe", frozenset({"GET", "HEAD"})),
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
        SDK REST operations. The handler uses in-memory state only until the
        M0.1 persistence boundary is implemented.
    """

    card = build_agent_card()
    handler = DefaultRequestHandler(
        agent_executor=RuntimeBootstrapExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=card,
        queue_manager=InMemoryQueueManager(),
    )
    return [
        *create_agent_card_routes(card),
        *_allowed_rest_routes(create_rest_routes(handler, path_prefix=A2A_PATH_PREFIX)),
    ]


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
    "build_agent_card",
    "build_runtime_routes",
    "is_a2a_path",
    "service_token",
]
