"""개발용 Gmail 동기화 트리거(`/api/v1/dev/gmail-sync`) 계약 테스트.

실제 Google 계정을 호출하지 않는다. `_load_authorized_session`만 Fake로
바꿔 Gmail Adapter 이후의 발행 경로(제목 조립·proposal_hub 발행·응답
형태)를 검증한다.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.dev_gmail_sync_api import (
    DEV_GMAIL_SYNC_ENABLED_ENV,
    GMAIL_PUSH_SECRET_ENV,
    GMAIL_PUSH_TOPIC_ENV,
    _watch_state,
)
from app.domain.task import TaskRecord
from app.email_action_items import EmailActionItem, EmailActionItemProviderError
from app.internal_chat import _authenticated_user_or_assignee
from app.main import app
from app.proposal_api import ignored_proposal_repository, proposal_hub
from app.providers.google import SyncCursorExpiredError
from app.task_api import task_repository


class FakeResponse:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self.body = body

    def json(self) -> dict:
        return self.body


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = iter(responses)

    def request(self, method: str, url: str, **kwargs):
        return next(self.responses)


def _full_message_response(message_id: str, subject: str, plain_body: str, snippet: str = "") -> FakeResponse:
    """`format=full` 응답(get_message_with_body가 읽는 모양)을 만든다."""

    return FakeResponse(200, {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "snippet": snippet,
        "payload": {
            "headers": [{"name": "Subject", "value": subject}] if subject else [],
            "mimeType": "text/plain",
            "body": {"data": base64.urlsafe_b64encode(plain_body.encode()).decode()},
        },
    })


class DevGmailSyncApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.database_url = os.environ.pop("DATABASE_URL", None)
        os.environ["WORKMATE_TASK_DB_PATH"] = os.path.join(cls.temp_dir.name, "tasks.sqlite3")
        os.environ["WORKMATE_IGNORED_PROPOSALS_DB_PATH"] = os.path.join(cls.temp_dir.name, "ignored_proposals.sqlite3")
        task_repository.cache_clear()
        ignored_proposal_repository.cache_clear()
        app.dependency_overrides[_authenticated_user_or_assignee] = lambda: "user-a"
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        app.dependency_overrides.pop(_authenticated_user_or_assignee, None)
        task_repository.cache_clear()
        ignored_proposal_repository.cache_clear()
        if cls.database_url is not None:
            os.environ["DATABASE_URL"] = cls.database_url
        os.environ.pop("WORKMATE_TASK_DB_PATH", None)
        os.environ.pop("WORKMATE_IGNORED_PROPOSALS_DB_PATH", None)
        cls.temp_dir.cleanup()

    def setUp(self) -> None:
        proposal_hub.clear()
        _watch_state.clear()
        self._previous_flag = os.environ.get(DEV_GMAIL_SYNC_ENABLED_ENV)
        self._previous_topic = os.environ.get(GMAIL_PUSH_TOPIC_ENV)
        self._previous_secret = os.environ.get(GMAIL_PUSH_SECRET_ENV)

    def tearDown(self) -> None:
        proposal_hub.clear()
        _watch_state.clear()
        for env_name, previous in (
            (DEV_GMAIL_SYNC_ENABLED_ENV, self._previous_flag),
            (GMAIL_PUSH_TOPIC_ENV, self._previous_topic),
            (GMAIL_PUSH_SECRET_ENV, self._previous_secret),
        ):
            if previous is None:
                os.environ.pop(env_name, None)
            else:
                os.environ[env_name] = previous

    def test_disabled_by_default_returns_404(self) -> None:
        os.environ.pop(DEV_GMAIL_SYNC_ENABLED_ENV, None)
        response = self.client.post("/api/v1/dev/gmail-sync")
        self.assertEqual(response.status_code, 404)

    def test_missing_token_file_returns_actionable_503(self) -> None:
        os.environ[DEV_GMAIL_SYNC_ENABLED_ENV] = "true"
        with patch("app.dev_gmail_sync_api._credential_paths", return_value=(
            __import__("pathlib").Path("does-not-exist-secret.json"),
            __import__("pathlib").Path("does-not-exist-token.json"),
        )):
            response = self.client.post("/api/v1/dev/gmail-sync")
        self.assertEqual(response.status_code, 503)
        self.assertIn("auth_flow.py", response.json()["detail"])

    def test_one_email_with_multiple_action_items_becomes_separate_proposals(self) -> None:
        """메일 하나에서 LLM이 할 일을 여러 개 뽑으면 별도 카드(제안) 여러 개가 된다."""

        os.environ[DEV_GMAIL_SYNC_ENABLED_ENV] = "true"
        fake_session = FakeSession([
            FakeResponse(200, {"messages": [{"id": "m-1", "threadId": "t-1"}]}),
            _full_message_response("m-1", "신규 펫 합성 시스템 기획서 (v1.0) 공유", "리뷰 미팅을 진행하고 일정을 확정해 주세요."),
        ])
        fake_items = [
            EmailActionItem(title="기획서 리뷰 미팅 참석", reason="리뷰 미팅 진행을 요청함"),
            EmailActionItem(title="개발 가능 일정 회신", reason="일정 산정을 명시적으로 요청함"),
        ]
        with patch("app.dev_gmail_sync_api._load_authorized_session", return_value=fake_session), \
                patch("app.dev_gmail_sync_api.extract_action_items", return_value=fake_items):
            response = self.client.post("/api/v1/dev/gmail-sync?limit=5")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["fetched"], 1)
        self.assertEqual(len(body["published"]), 2)
        self.assertEqual(body["published"][0]["title"], "기획서 리뷰 미팅 참석")
        self.assertEqual(body["published"][1]["title"], "개발 가능 일정 회신")
        self.assertIsNotNone(proposal_hub.get("user-a", "email", "m-1:0"))
        self.assertIsNotNone(proposal_hub.get("user-a", "email", "m-1:1"))

    def test_already_approved_message_is_skipped_without_llm_call(self) -> None:
        """이미 승인해 Task가 있는 메일은 재동기화 시 다시 분석·발행하지 않는다."""

        task_repository().create(
            TaskRecord(
                task_id="task-1",
                assignee_user_id="user-a",
                title="이미 처리한 항목",
                source_type="email",
                source_id="m-done:0",
            )
        )
        os.environ[DEV_GMAIL_SYNC_ENABLED_ENV] = "true"
        fake_session = FakeSession([FakeResponse(200, {"messages": [{"id": "m-done", "threadId": "t-done"}]})])
        with patch("app.dev_gmail_sync_api._load_authorized_session", return_value=fake_session), \
                patch("app.dev_gmail_sync_api.extract_action_items") as mock_extract:
            response = self.client.post("/api/v1/dev/gmail-sync?limit=5")
        mock_extract.assert_not_called()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["published"], [])
        self.assertEqual(body["skipped"], [{"message_id": "m-done", "reason": "already_approved"}])

    def test_already_ignored_message_is_skipped_without_llm_call(self) -> None:
        """"무시"했던 메일도 승인된 메일과 같은 이유로 재동기화 시 다시
        분석·발행하지 않는다 — 예전엔 이 기록 자체가 없어 재동기화 버튼을
        누를 때마다 이미 무시한 후보가 그대로 다시 노출됐다(2026-08-16,
        17번 갭 문서 #2)."""

        ignored_proposal_repository().mark("user-a", "email", "m-ignored")
        os.environ[DEV_GMAIL_SYNC_ENABLED_ENV] = "true"
        fake_session = FakeSession([FakeResponse(200, {"messages": [{"id": "m-ignored", "threadId": "t-ignored"}]})])
        with patch("app.dev_gmail_sync_api._load_authorized_session", return_value=fake_session), \
                patch("app.dev_gmail_sync_api.extract_action_items") as mock_extract:
            response = self.client.post("/api/v1/dev/gmail-sync?limit=5")
        mock_extract.assert_not_called()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["published"], [])
        self.assertEqual(body["skipped"], [{"message_id": "m-ignored", "reason": "already_ignored"}])

    def test_ignoring_a_proposal_marks_it_so_future_syncs_skip_it(self) -> None:
        """"무시" 결정이 실제로 `ignored_proposal_repository`에 기록되는지
        Review 엔드포인트를 통해 확인한다(Repository 직접 호출이 아니라
        실제 사용자 경로로)."""

        import asyncio

        from app.proposal_api import TaskProposal, proposal_hub

        proposal = TaskProposal(
            proposal_id="email-m-review",
            user_id="user-a",
            source_type="email",
            source_id="m-review",
            title="검토용 제안",
            assignee_user_id="user-a",
        )
        asyncio.run(proposal_hub.publish(proposal))
        response = self.client.post(
            "/api/v1/email-task-proposals:review",
            json={"message_id": "m-review", "decision": "ignore"},
            headers={"Idempotency-Key": "test-ignore-key-1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("m-review", ignored_proposal_repository().ignored_source_ids("user-a", "email"))

    def test_email_with_no_actionable_content_publishes_nothing(self) -> None:
        """광고·전달 메일처럼 LLM이 할 일을 못 찾으면 카드를 만들지 않는다."""

        os.environ[DEV_GMAIL_SYNC_ENABLED_ENV] = "true"
        fake_session = FakeSession([
            FakeResponse(200, {"messages": [{"id": "m-ad", "threadId": "t-ad"}]}),
            _full_message_response("m-ad", "이번 달 뉴스레터", "이번 달 소식을 전해드립니다."),
        ])
        with patch("app.dev_gmail_sync_api._load_authorized_session", return_value=fake_session), \
                patch("app.dev_gmail_sync_api.extract_action_items", return_value=[]):
            response = self.client.post("/api/v1/dev/gmail-sync?limit=5")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["fetched"], 1)
        self.assertEqual(body["published"], [])
        self.assertIsNone(proposal_hub.get("user-a", "email", "m-ad:0"))

    def test_llm_failure_skips_message_instead_of_falling_back_to_every_email(self) -> None:
        """LLM 호출이 실패하면 예전처럼 메일을 통째로 제안하지 않고 건너뛴다."""

        os.environ[DEV_GMAIL_SYNC_ENABLED_ENV] = "true"
        fake_session = FakeSession([
            FakeResponse(200, {"messages": [{"id": "m-err", "threadId": "t-err"}]}),
            _full_message_response("m-err", "제목", "본문"),
        ])
        with patch("app.dev_gmail_sync_api._load_authorized_session", return_value=fake_session), \
                patch("app.dev_gmail_sync_api.extract_action_items", side_effect=EmailActionItemProviderError("provider down")):
            response = self.client.post("/api/v1/dev/gmail-sync?limit=5")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["published"], [])
        self.assertEqual(len(body["skipped"]), 1)
        self.assertEqual(body["skipped"][0]["message_id"], "m-err")
        self.assertIn("llm_extraction_failed", body["skipped"][0]["reason"])

    def test_requires_authentication(self) -> None:
        os.environ[DEV_GMAIL_SYNC_ENABLED_ENV] = "true"
        app.dependency_overrides.pop(_authenticated_user_or_assignee, None)
        try:
            response = self.client.post("/api/v1/dev/gmail-sync")
            self.assertEqual(response.status_code, 401)
        finally:
            app.dependency_overrides[_authenticated_user_or_assignee] = lambda: "user-a"

    def test_register_watch_requires_topic_configuration(self) -> None:
        os.environ[DEV_GMAIL_SYNC_ENABLED_ENV] = "true"
        os.environ.pop(GMAIL_PUSH_TOPIC_ENV, None)
        response = self.client.post("/api/v1/dev/gmail-watch/register")
        self.assertEqual(response.status_code, 503)

    def test_register_watch_stores_state_for_push(self) -> None:
        os.environ[DEV_GMAIL_SYNC_ENABLED_ENV] = "true"
        os.environ[GMAIL_PUSH_TOPIC_ENV] = "projects/test-project/topics/gmail-push"
        fake_session = FakeSession([
            FakeResponse(200, {"emailAddress": "user@example.com", "historyId": "100"}),
            FakeResponse(200, {"historyId": "101", "expiration": "1999999999000"}),
        ])
        with patch("app.dev_gmail_sync_api._load_authorized_session", return_value=fake_session):
            response = self.client.post("/api/v1/dev/gmail-watch/register")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["email"], "user@example.com")
        self.assertEqual(body["history_id"], "101")
        self.assertEqual(_watch_state["user@example.com"], {"user_id": "user-a", "last_history_id": "101"})

    def test_watch_status_reports_in_memory_registrations(self) -> None:
        os.environ[DEV_GMAIL_SYNC_ENABLED_ENV] = "true"
        response = self.client.get("/api/v1/dev/gmail-watch/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"watches": []})

        _watch_state["user@example.com"] = {"user_id": "user-a", "last_history_id": "100"}
        response = self.client.get("/api/v1/dev/gmail-watch/status")
        self.assertEqual(response.json(), {"watches": [{"email": "user@example.com", "user_id": "user-a", "last_history_id": "100"}]})

    def test_push_rejects_missing_or_wrong_secret(self) -> None:
        os.environ[DEV_GMAIL_SYNC_ENABLED_ENV] = "true"
        os.environ[GMAIL_PUSH_SECRET_ENV] = "correct-secret"
        response = self.client.post("/api/v1/dev/gmail-push", json={"message": {"data": "x"}})
        self.assertEqual(response.status_code, 401)
        response = self.client.post("/api/v1/dev/gmail-push?secret=wrong", json={"message": {"data": "x"}})
        self.assertEqual(response.status_code, 401)

    def _pubsub_envelope(self, email: str, history_id: str) -> dict:
        data = json.dumps({"emailAddress": email, "historyId": history_id}).encode()
        return {"message": {"data": base64.b64encode(data).decode(), "messageId": "pubsub-1"}, "subscription": "projects/test/subscriptions/sub"}

    def test_push_ignores_unregistered_email_without_error(self) -> None:
        os.environ[DEV_GMAIL_SYNC_ENABLED_ENV] = "true"
        os.environ[GMAIL_PUSH_SECRET_ENV] = "s3cret"
        response = self.client.post(
            "/api/v1/dev/gmail-push?secret=s3cret",
            json=self._pubsub_envelope("unknown@example.com", "200"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ignored")

    def test_push_fetches_new_messages_and_publishes_action_item_proposals(self) -> None:
        os.environ[DEV_GMAIL_SYNC_ENABLED_ENV] = "true"
        os.environ[GMAIL_PUSH_SECRET_ENV] = "s3cret"
        _watch_state["user@example.com"] = {"user_id": "user-a", "last_history_id": "100"}
        fake_session = FakeSession([
            FakeResponse(200, {"history": [{"messagesAdded": [{"message": {"id": "m-9", "threadId": "t-9"}}]}]}),
            _full_message_response("m-9", "긴급: 배포 승인 요청", "오늘 중으로 배포를 승인해 주세요."),
        ])
        fake_items = [EmailActionItem(title="배포 승인", reason="오늘까지 승인을 요청함")]
        with patch("app.dev_gmail_sync_api._load_authorized_session", return_value=fake_session), \
                patch("app.dev_gmail_sync_api.extract_action_items", return_value=fake_items):
            response = self.client.post(
                "/api/v1/dev/gmail-push?secret=s3cret",
                json=self._pubsub_envelope("user@example.com", "105"),
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["published"], [{"message_id": "m-9", "source_id": "m-9:0", "title": "배포 승인"}])
        self.assertEqual(_watch_state["user@example.com"]["last_history_id"], "105")
        self.assertIsNotNone(proposal_hub.get("user-a", "email", "m-9:0"))

    def test_push_skips_message_when_no_action_items_found(self) -> None:
        os.environ[DEV_GMAIL_SYNC_ENABLED_ENV] = "true"
        os.environ[GMAIL_PUSH_SECRET_ENV] = "s3cret"
        _watch_state["user@example.com"] = {"user_id": "user-a", "last_history_id": "100"}
        fake_session = FakeSession([
            FakeResponse(200, {"history": [{"messagesAdded": [{"message": {"id": "m-fyi", "threadId": "t-fyi"}}]}]}),
            _full_message_response("m-fyi", "참고용 공유", "참고하시라고 공유드립니다."),
        ])
        with patch("app.dev_gmail_sync_api._load_authorized_session", return_value=fake_session), \
                patch("app.dev_gmail_sync_api.extract_action_items", return_value=[]):
            response = self.client.post(
                "/api/v1/dev/gmail-push?secret=s3cret",
                json=self._pubsub_envelope("user@example.com", "106"),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["published"], [])
        self.assertIsNone(proposal_hub.get("user-a", "email", "m-fyi:0"))

    def test_push_resets_cursor_on_expired_history(self) -> None:
        os.environ[DEV_GMAIL_SYNC_ENABLED_ENV] = "true"
        os.environ[GMAIL_PUSH_SECRET_ENV] = "s3cret"
        _watch_state["user@example.com"] = {"user_id": "user-a", "last_history_id": "50"}

        class ExpiringSession:
            def request(self, method, url, **kwargs):
                raise SyncCursorExpiredError()

        with patch("app.dev_gmail_sync_api._load_authorized_session", return_value=ExpiringSession()):
            response = self.client.post(
                "/api/v1/dev/gmail-push?secret=s3cret",
                json=self._pubsub_envelope("user@example.com", "999"),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "cursor_reset")
        self.assertEqual(_watch_state["user@example.com"]["last_history_id"], "999")


if __name__ == "__main__":
    unittest.main()
