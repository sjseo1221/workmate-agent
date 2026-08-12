"""M0.1-02 contract tests for the public SDK transport boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
import unittest

from fastapi.testclient import TestClient
from google.protobuf import json_format
from jsonschema import Draft202012Validator, FormatChecker

from a2a.types import Message, Part, Role
from app.main import app


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


if __name__ == "__main__":
    unittest.main()
