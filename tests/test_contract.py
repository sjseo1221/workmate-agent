"""M0.1-02 contract tests for the public SDK transport boundary."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from google.protobuf import json_format
from jsonschema import Draft202012Validator, FormatChecker

from a2a.server.context import ServerCallContext
from a2a.server.events import InMemoryQueueManager
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types import (
    Message,
    Part,
    Role,
    SubscribeToTaskRequest,
    Task,
    TaskState,
    TaskStatus,
)
from app.main import app
from app.a2a import runtime
from app.a2a.persistence import PersistentTaskStore
from app.a2a.runtime import RuntimeBootstrapExecutor, build_agent_card
from app.domain.meeting import MeetingRecord
from app.meeting_analysis import ActionItem, MeetingAnalysis
from app.repositories.meeting_chunks import HybridSearchHit
from app.repositories.meetings import SQLiteMeetingRepository
from app.workflows.meetings import (
    build_analyze_meeting_workflow,
    build_search_meetings_workflow,
)
from app.workflows.registry import WorkflowRegistry


os.environ.setdefault("WORKMATE_SERVICE_TOKEN", "test-token")


def _schema_root() -> Path:
    """Locate the superproject's approved schema directory."""

    configured = os.getenv("WORKMATE_SCHEMA_ROOT")
    candidates = [
        Path(configured) if configured else None,
        Path(__file__).resolve().parents[2] / "docs" / "schemas",
    ]
    for candidate in candidates:
        if candidate and (candidate / "a2a-artifact.schema.json").is_file():
            return candidate
    raise AssertionError(
        "Approved schemas are unavailable; set WORKMATE_SCHEMA_ROOT to the "
        "superproject docs/schemas directory."
    )


def _validator(filename: str) -> Draft202012Validator:
    """Load one approved Draft 2020-12 schema with format checking enabled."""

    schema = json.loads((_schema_root() / filename).read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _sdk_message(skill_id: str, data: dict[str, object]) -> dict[str, object]:
    """Serialize an SDK ``Message`` as the public HTTP+JSON request body."""

    message = Message(
        message_id=f"contract-{skill_id}",
        role=Role.ROLE_USER,
        parts=[Part(media_type="application/json")],
    )
    json_format.ParseDict(data, message.parts[0].data)
    return {
        "message": json_format.MessageToDict(
            message, preserving_proto_field_name=False
        ),
        "metadata": {"request_id": f"request-{skill_id}"},
    }


def _skill_inputs() -> list[dict[str, object]]:
    """Return one valid approved input for every public Workmate Skill."""

    common = {
        "schema_version": "1.0",
        "user_id": "contract-user",
        "timezone": "Asia/Seoul",
        "locale": "ko-KR",
    }
    return [
        {**common, "skill_id": "daily_briefing", "as_of": "2026-08-12T09:00:00+09:00"},
        {**common, "skill_id": "weekly_report", "week_of": "2026-08-10"},
        {**common, "skill_id": "analyze_meeting", "meeting_id": "meeting-1"},
        {**common, "skill_id": "search_meetings", "query": "decision", "limit": 5},
        {**common, "skill_id": "rank_priorities", "as_of": "2026-08-12T09:00:00+09:00"},
    ]


def _result_envelope() -> dict[str, object]:
    """Return a minimal non-mock result that satisfies the approved schema."""

    return {
        "schema_version": "1.0",
        "request_id": "request-daily-1",
        "generated_at": "2026-08-12T00:00:00Z",
        "data_freshness": {"task": None, "calendar": None, "email": None, "meeting": None},
        "warnings": [],
        "result": {
            "type": "daily_briefing",
            "data": {
                "date": "2026-08-12",
                "summary": "No provider data was requested in this contract fixture.",
                "calendar_events": [],
                "important_signals": [],
                "priorities": [],
                "source_refs": [],
            },
        },
    }


class PublicA2AContractTests(unittest.TestCase):
    """Verify that the public boundary uses SDK types and approved schemas."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)
        cls.headers = {
            "Authorization": "Bearer test-token",
            "A2A-Version": "1.0",
            "Content-Type": "application/a2a+json",
        }

    def test_all_skill_inputs_validate_against_approved_schema(self) -> None:
        validator = _validator("workmate-skill-schemas.schema.json")
        for data in _skill_inputs():
            errors = list(validator.iter_errors(data))
            self.assertEqual(errors, [], data["skill_id"])

    def test_agent_card_cache_control_is_public_for_one_hour(self) -> None:
        response = self.client.get("/.well-known/agent-card.json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("public", response.headers.get("cache-control", ""))
        self.assertIn("max-age=3600", response.headers.get("cache-control", ""))

    def test_result_envelope_and_runtime_artifact_validate(self) -> None:
        _validator("workmate-skill-schemas.schema.json").validate(_result_envelope())
        response = self.client.post(
            "/a2a/message:send",
            json=_sdk_message("daily_briefing", _skill_inputs()[0]),
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        task = response.json()["task"]
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        for artifact in task["artifacts"]:
            _validator("a2a-artifact.schema.json").validate(artifact)
            self.assertFalse(any("mock" in part for part in artifact["parts"]))

    def test_sdk_transport_routes_do_not_expose_legacy_or_extra_routes(self) -> None:
        self.assertFalse(Path(__file__).parents[1].joinpath("app.py").exists())
        self.assertFalse(Path(__file__).parents[1].joinpath("smoke_test.py").exists())
        paths = {route.path for route in app.routes if route.path.startswith("/a2a/")}
        self.assertNotIn("/a2a/tasks", paths)
        self.assertNotIn("/a2a/extendedAgentCard", paths)
        self.assertFalse(any("pushNotification" in path for path in paths))

    def test_sdk_message_stream_uses_event_stream(self) -> None:
        response = self.client.post(
            "/a2a/message:stream",
            json=_sdk_message("weekly_report", _skill_inputs()[1]),
            headers={**self.headers, "Accept": "text/event-stream"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertIn('"task"', response.text)

    def test_all_five_skills_execute_through_public_route(self) -> None:
        """실제 Workflow 코드와 명시적 Provider Fixture로 최초 5개 Skill을 검증한다.

        2026-08-17 이후 Agent Card는 11개 Skill을 공개하지만(15번 문서 결정),
        나머지 6개(`get_meeting_analysis` 등)는 외부 Provider(OpenAI·Gmail)나
        선행 데이터가 필요해 여기 추가하지 않았다 — 각 Skill Workflow 자체의
        단위 테스트가 별도로 있고, 공개 A2A 경로로도 실행 가능함은 이 계약
        테스트가 쓰는 것과 같은 `/a2a/message:send`·`registry.execute()`
        경로이므로 동일하게 성립한다(Agent Card 등재 여부는 SDK가 skill_id를
        걸러내는 관문이 아니다)."""

        class ContractEmbeddingProvider:
            def embed(self, texts):
                return [(0.1,) * 1536 for _ in texts]

        class ContractSearchRepository:
            def search_hybrid(self, query, query_embedding, user_id, **kwargs):
                return [
                    HybridSearchHit(
                        meeting_chunk_id="chunk-contract-1",
                        meeting_id="meeting-1",
                        user_id=user_id,
                        content="예산은 1천만원으로 결정했다.",
                        meeting_title="계약 검증 회의",
                        meeting_started_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
                        sequence_no=0,
                        rrf_score=0.03,
                        dense_rank=1,
                        keyword_rank=1,
                    )
                ]

        def contract_analyzer(transcript: str) -> MeetingAnalysis:
            self.assertIn("결과를 내일까지 공유한다.", transcript)
            return MeetingAnalysis(
                title="결과 공유 회의",
                summary="공유 일정을 확정했다.",
                action_items=[
                    ActionItem(
                        action_item_id="action-contract-1",
                        title="결과 공유",
                        evidence_text="결과를 내일까지 공유한다.",
                        start_ms=0,
                        end_ms=1000,
                    )
                ],
            )

        registry = runtime._WORKFLOW_REGISTRY
        self.assertIsNotNone(registry)
        assert registry is not None
        with tempfile.TemporaryDirectory() as directory:
            meeting_repository = SQLiteMeetingRepository(
                Path(directory) / "contract-meetings.sqlite3"
            )
            meeting_repository.create(
                MeetingRecord("meeting-1", "contract-user", "계약 검증 회의")
            )
            meeting_repository.save_transcript(
                "meeting-1",
                "contract-user",
                0,
                "결과를 내일까지 공유한다.",
                True,
                0,
                1000,
            )
            handlers = {
                "analyze_meeting": build_analyze_meeting_workflow(
                    repository=meeting_repository,
                    analyzer=contract_analyzer,
                ),
                "search_meetings": build_search_meetings_workflow(
                    repository=ContractSearchRepository(),
                    embedding_provider=ContractEmbeddingProvider(),
                ),
            }
            with patch.dict(registry._handlers, handlers):
                artifact_validator = _validator("a2a-artifact.schema.json")
                for data in _skill_inputs():
                    response = self.client.post(
                        "/a2a/message:send",
                        json=_sdk_message(str(data["skill_id"]), data),
                        headers=self.headers,
                    )
                    self.assertEqual(response.status_code, 200, data["skill_id"])
                    task = response.json()["task"]
                    self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
                    self.assertGreaterEqual(len(task.get("artifacts", [])), 1)
                    for artifact in task["artifacts"]:
                        artifact_validator.validate(artifact)
                        self.assertNotEqual(artifact["name"], "runtime_bootstrap")

    def test_authentication_and_version_failures_are_rejected(self) -> None:
        payload = _sdk_message("daily_briefing", _skill_inputs()[0])
        missing = self.client.post("/a2a/message:send", json=payload)
        self.assertEqual(missing.status_code, 401)
        wrong = self.client.post(
            "/a2a/message:send",
            json=payload,
            headers={**self.headers, "Authorization": "Bearer wrong-token"},
        )
        self.assertEqual(wrong.status_code, 401)
        missing_version = self.client.post(
            "/a2a/message:send",
            json=payload,
            headers={key: value for key, value in self.headers.items() if key != "A2A-Version"},
        )
        self.assertEqual(missing_version.status_code, 400)

    def test_stream_contains_task_artifact_and_terminal_events_in_order(self) -> None:
        response = self.client.post(
            "/a2a/message:stream",
            json=_sdk_message("weekly_report", _skill_inputs()[1]),
            headers={**self.headers, "Accept": "text/event-stream"},
        )
        self.assertEqual(response.status_code, 200)
        task_index = response.text.index('"task"')
        artifact_index = response.text.index('"artifactUpdate"')
        completed_index = response.text.rindex('"statusUpdate"')
        self.assertLess(task_index, artifact_index)
        self.assertLess(artifact_index, completed_index)

    def test_get_subscribe_is_not_public_and_terminal_post_subscribe_errors(self) -> None:
        response = self.client.post(
            "/a2a/message:send",
            json=_sdk_message("daily_briefing", _skill_inputs()[0]),
            headers=self.headers,
        )
        task_id = response.json()["task"]["id"]
        get_subscribe = self.client.get(
            f"/a2a/tasks/{task_id}:subscribe", headers=self.headers
        )
        self.assertIn(get_subscribe.status_code, {404, 405})
        post_subscribe = self.client.post(
            f"/a2a/tasks/{task_id}:subscribe", headers=self.headers
        )
        self.assertIn(post_subscribe.status_code, {400, 409, 422})

    def test_active_subscribe_emits_current_task_snapshot(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                store = PersistentTaskStore(Path(directory) / "a2a.sqlite3")
                queue_manager = InMemoryQueueManager()
                handler = DefaultRequestHandler(
                    agent_executor=RuntimeBootstrapExecutor(
                        store, WorkflowRegistry()
                    ),
                    task_store=store,
                    agent_card=build_agent_card(),
                    queue_manager=queue_manager,
                )
                context = ServerCallContext()
                task = Task(
                    id="active-subscribe-task",
                    context_id="active-subscribe-context",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
                await store.save(task, context)
                await queue_manager.create_or_tap(task.id)
                events = handler.on_subscribe_to_task(
                    SubscribeToTaskRequest(id=task.id), context
                )
                first = await anext(events)
                self.assertEqual(first.id, task.id)
                await events.aclose()

        asyncio.run(scenario())


class ArtifactPartsTextFallbackTests(unittest.TestCase):
    """`text`만 읽는 Client도 구조화 결과를 볼 수 있는지 검증한다."""

    def test_structured_result_without_markdown_still_carries_text_part(self) -> None:
        """search_meetings·analyze_meeting처럼 data만 있는 결과도 text Part를 낸다."""

        from app.workflows.registry import WorkflowResult

        result = WorkflowResult(
            artifact_name="grounded_answer",
            artifact_description="Grounded meeting answer with exact chunk citations.",
            text=json.dumps({"type": "grounded_answer", "data": {"answer": "근거 기반 답변"}}, ensure_ascii=False),
            data={"type": "grounded_answer", "data": {"answer": "근거 기반 답변"}},
        )

        parts = runtime._artifact_parts(result)

        self.assertTrue(
            any(part.text for part in parts),
            "data만 있는 결과는 최소 하나의 text Part를 포함해야 legacy client가 읽을 수 있다",
        )

    def test_markdown_result_is_unaffected(self) -> None:
        """Markdown이 있는 기존 결과(weekly_report)는 중복 text Part를 만들지 않는다."""

        from app.workflows.registry import WorkflowResult

        result = WorkflowResult(
            artifact_name="weekly_report",
            artifact_description="Weekly report artifact.",
            text=json.dumps({"type": "weekly_report"}, ensure_ascii=False),
            data={"type": "weekly_report"},
            markdown="# 주간 보고서",
        )

        parts = runtime._artifact_parts(result)

        text_parts = [part for part in parts if part.text]
        self.assertEqual(len(text_parts), 1)
        self.assertEqual(text_parts[0].text, "# 주간 보고서")


if __name__ == "__main__":
    unittest.main()
