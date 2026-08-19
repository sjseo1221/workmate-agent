"""M1.4 메일·Calendar 제안 승인·무시·중복 계약 테스트."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.domain.task import TaskRecord
from app.internal_chat import _authenticated_user_or_assignee
from app.main import app
from app.providers.google import CalendarEvent, GmailMessage
from app.proposal_api import (
    publish_calendar_proposal,
    publish_email_proposal,
    proposal_hub,
)
from app.repositories.sync_state import SQLiteSyncStateRepository
from app.task_api import task_repository
from app.workflows.external_sync import ExternalSyncWorkflow, SyncBatch


class ProposalApiTests(unittest.TestCase):
    """인증 사용자 범위와 승인 규칙을 검증한다."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.database_url = os.environ.pop("DATABASE_URL", None)
        os.environ["WORKMATE_TASK_DB_PATH"] = os.path.join(cls.temp_dir.name, "tasks.sqlite3")
        task_repository.cache_clear()
        # proposal_api.py의 모든 라우트와 `GET /api/v1/tasks`(task_api.py) 둘 다
        # `_authenticated_user_or_assignee`에 의존한다(2026-08-19 — proposal_api.py도
        # 담당자-이름 인증을 허용하도록 `_authenticated_user`에서 옮겨왔다, 아래
        # 참고). 하나만 override하면 된다.
        app.dependency_overrides[_authenticated_user_or_assignee] = lambda: "user-a"
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        app.dependency_overrides.pop(_authenticated_user_or_assignee, None)
        proposal_hub.clear()
        task_repository.cache_clear()
        if cls.database_url is not None:
            os.environ["DATABASE_URL"] = cls.database_url
        os.environ.pop("WORKMATE_TASK_DB_PATH", None)
        cls.temp_dir.cleanup()

    def setUp(self) -> None:
        proposal_hub.clear()

    def test_email_approval_is_idempotent_and_persists_only_after_approval(self) -> None:
        asyncio.run(
            publish_email_proposal(
                "user-a",
                GmailMessage("message-1", "thread-1", "회신 요청", ("INBOX",)),
                title="메일 회신",
            )
        )
        payload = {
            "message_id": "message-1",
            "decision": "approve",
            "task": {"title": "메일 회신", "assignee_user_id": "user-a", "priority_hint": 7},
        }
        first = self.client.post("/api/v1/email-task-proposals:review", json=payload, headers={"Idempotency-Key": "email-1"})
        second = self.client.post("/api/v1/email-task-proposals:review", json=payload, headers={"Idempotency-Key": "email-1"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(first.json()["created"])
        self.assertEqual(first.json(), second.json())
        tasks = [item for item in self.client.get("/api/v1/tasks").json() if item["source_id"] == "message-1"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["source_id"], "message-1")

    def test_email_proposal_metadata_carries_received_at_for_card_display_and_sort(self) -> None:
        """카드에 수신 날짜를 보여주고 정렬하려면 `metadata.received_at`이
        필요하다(2026-08-17 요청) — `due_at`은 email 제안에서 항상 `null`이라
        재사용하지 않는다."""

        asyncio.run(
            publish_email_proposal(
                "user-a",
                GmailMessage("message-received", "thread-1", "회신 요청", ("INBOX",), None, "2026-08-15T09:30:00+00:00"),
                title="메일 회신",
            )
        )
        proposal = proposal_hub.get("user-a", "email", "message-received")
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.metadata["received_at"], "2026-08-15T09:30:00+00:00")
        self.assertIsNone(proposal.due_at)

    def test_ignore_does_not_create_task(self) -> None:
        asyncio.run(
            publish_email_proposal(
                "user-a",
                GmailMessage("message-ignore", None, "알림", ("INBOX",)),
                title="무시할 메일",
            )
        )
        response = self.client.post(
            "/api/v1/email-task-proposals:review",
            json={"message_id": "message-ignore", "decision": "ignore"},
            headers={"Idempotency-Key": "ignore-1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["task"])
        self.assertFalse(any(item["source_id"] == "message-ignore" for item in self.client.get("/api/v1/tasks").json()))

    def test_calendar_source_id_and_user_scope(self) -> None:
        event = CalendarEvent("primary", "event-1", "회의 후속", "confirmed", None, None, None)
        asyncio.run(publish_calendar_proposal("user-a", event))
        app.dependency_overrides[_authenticated_user_or_assignee] = lambda: "user-b"
        denied = self.client.post(
            "/api/v1/calendar-task-proposals:review",
            json={
                "calendar_id": "primary",
                "event_id": "event-1",
                "decision": "approve",
                "task": {"title": "회의 후속", "assignee_user_id": "user-b"},
            },
            headers={"Idempotency-Key": "calendar-other-user"},
        )
        self.assertEqual(denied.status_code, 404)
        app.dependency_overrides[_authenticated_user_or_assignee] = lambda: "user-a"
        approved = self.client.post(
            "/api/v1/calendar-task-proposals:review",
            json={
                "calendar_id": "primary",
                "event_id": "event-1",
                "decision": "approve",
                "task": {"title": "회의 후속", "assignee_user_id": "user-a"},
            },
            headers={"Idempotency-Key": "calendar-1"},
        )
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["task"]["source_id"], "primary:event-1")

    def test_similar_task_requires_explicit_reconfirmation(self) -> None:
        due_at = datetime(2026, 8, 20, 9, tzinfo=timezone.utc)
        task_repository().create(
            TaskRecord("manual-similar", "user-a", "같은 제목", due_at=due_at)
        )
        event = CalendarEvent("primary", "event-similar", "일정", "confirmed", None, None, None)
        asyncio.run(publish_calendar_proposal("user-a", event, title="같은 제목", due_at=due_at))
        payload = {
            "calendar_id": "primary",
            "event_id": "event-similar",
            "decision": "approve",
            "task": {"title": "같은 제목", "assignee_user_id": "user-a", "due_at": due_at.isoformat()},
        }
        rejected = self.client.post("/api/v1/calendar-task-proposals:review", json=payload, headers={"Idempotency-Key": "similar-1"})
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.json()["detail"]["code"], "SIMILAR_TASK_EXISTS")
        payload["allow_similar_duplicate"] = True
        approved = self.client.post("/api/v1/calendar-task-proposals:review", json=payload, headers={"Idempotency-Key": "similar-2"})
        self.assertEqual(approved.status_code, 200)

    def test_sse_event_contains_user_scoped_proposal(self) -> None:
        async def read_event() -> str:
            await publish_email_proposal(
                "user-sse",
                GmailMessage("message-sse", None, "SSE", ()),
                title="SSE 제안",
            )
            stream = proposal_hub.events("user-sse")
            event = await stream.__anext__()
            await stream.aclose()
            return event

        event = asyncio.run(read_event())
        self.assertIn("event: task_proposal", event)
        self.assertIn('"source_id": "message-sse"', event)

    def test_external_sync_publishes_only_selected_candidates(self) -> None:
        workflow = ExternalSyncWorkflow(SQLiteSyncStateRepository())
        message = GmailMessage("message-selected", None, "회신", ())
        ignored = GmailMessage("message-ignored", None, "알림", ())
        event = CalendarEvent("primary", "event-selected", "회의 후속", "confirmed", None, None, None)
        ids = asyncio.run(
            workflow.publish_task_proposals(
                "user-a",
                SyncBatch(items=(message, ignored, event), next_cursor="cursor-1"),
                titles={"message-selected": "메일 회신", "primary:event-selected": "회의 후속"},
            )
        )
        self.assertEqual(ids, ("message-selected", "primary:event-selected"))
        self.assertIsNotNone(proposal_hub.get("user-a", "email", "message-selected"))
        self.assertIsNone(proposal_hub.get("user-a", "email", "message-ignored"))
        self.assertIsNotNone(proposal_hub.get("user-a", "calendar", "primary:event-selected"))

    def test_real_gmail_style_proposal_approves_into_a_correct_task(self) -> None:
        """실제 Gmail Push에서 나온 것과 같은 모양(16진수 ID·긴 한글 제목·괄호)을 그대로 승인해본다."""

        message_id = "1a000a4c4544d174"
        subject = "신규 펫 합성 및 승급 시스템 기획서 (v1.0) 공유 Inbox"
        asyncio.run(
            publish_email_proposal(
                "user-a",
                GmailMessage(message_id, "thread-x", "안녕하세요, 게임기획팀입니다...", ("INBOX",)),
                title=subject,
            )
        )
        payload = {
            "message_id": message_id,
            "decision": "approve",
            "task": {"title": subject, "assignee_user_id": "user-a", "due_at": None, "priority_hint": None},
        }
        response = self.client.post(
            "/api/v1/email-task-proposals:review", json=payload, headers={"Idempotency-Key": f"gmail-{message_id}"}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["created"])
        self.assertEqual(body["task"]["title"], subject)
        self.assertEqual(body["task"]["source_type"], "email")
        self.assertEqual(body["task"]["source_id"], message_id)
        self.assertEqual(body["task"]["status"], "todo")
        # 승인 후 제안함에서 사라져 재조회되지 않는다.
        self.assertIsNone(proposal_hub.get("user-a", "email", message_id))
        # 할 일 관리 화면과 같은 목록 API에 그대로 나타난다.
        tasks = self.client.get("/api/v1/tasks").json()
        self.assertTrue(any(item["source_id"] == message_id and item["title"] == subject for item in tasks))

    def test_editing_assignee_to_someone_else_is_rejected(self) -> None:
        """승인 폼에서 담당자를 실제 로그인한 사용자와 다르게 바꾸면 거부된다."""

        asyncio.run(
            publish_email_proposal("user-a", GmailMessage("message-tamper", None, "요청", ()), title="담당자 변조 시도")
        )
        response = self.client.post(
            "/api/v1/email-task-proposals:review",
            json={
                "message_id": "message-tamper",
                "decision": "approve",
                "task": {"title": "담당자 변조 시도", "assignee_user_id": "someone-else"},
            },
            headers={"Idempotency-Key": "tamper-1"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(any(item["source_id"] == "message-tamper" for item in self.client.get("/api/v1/tasks").json()))

    def test_review_requires_idempotency_key(self) -> None:
        response = self.client.post(
            "/api/v1/email-task-proposals:review",
            json={"message_id": "message-none", "decision": "ignore"},
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
