"""Workmate 어시스턴트(Drawer) 전용 신규 Skill Workflow 계약 테스트(18번 문서).

기존 REST 함수를 그대로 호출하는 얇은 어댑터라, Mock 대신 실제 Repository로
검증한다 — 화면(REST)이 이미 검증한 로직을 Skill 쪽에서 다시 베끼지 않았는지
확인하는 것이 목적이다.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
import unittest.mock

from app.internal_chat import _authenticated_user
from app.main import app
from app.meeting_api import meeting_repository
from app.proposal_api import proposal_hub, publish_calendar_proposal, publish_email_proposal
from app.providers.google import CalendarEvent, GmailMessage
from app.task_api import task_repository
from app.workflows.assistant_skills import (
    get_meeting_analysis_workflow,
    manage_tasks_workflow,
    read_email_workflow,
    review_action_items_workflow,
    review_proposal_workflow,
)
from app.workflows.registry import WorkflowRequest


def _request(skill_id: str, payload: dict[str, object], *, user_id: str = "user-a") -> WorkflowRequest:
    return WorkflowRequest(
        skill_id=skill_id,
        task_id="task-run",
        thread_id="task-run",
        message_id="message-run",
        user_id=user_id,
        payload={**payload, "user_id": user_id},
    )


class AssistantSkillsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        app.dependency_overrides[_authenticated_user] = lambda: "user-a"

    @classmethod
    def tearDownClass(cls) -> None:
        app.dependency_overrides.pop(_authenticated_user, None)

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self._previous_database_url = os.environ.pop("DATABASE_URL", None)
        os.environ["WORKMATE_TASK_DB_PATH"] = os.path.join(self.temp_dir.name, "tasks.sqlite3")
        os.environ["WORKMATE_MEETING_DB_PATH"] = os.path.join(self.temp_dir.name, "meetings.sqlite3")
        task_repository.cache_clear()
        meeting_repository.cache_clear()
        proposal_hub.clear()

    def tearDown(self) -> None:
        task_repository.cache_clear()
        meeting_repository.cache_clear()
        proposal_hub.clear()
        os.environ.pop("WORKMATE_TASK_DB_PATH", None)
        os.environ.pop("WORKMATE_MEETING_DB_PATH", None)
        if self._previous_database_url is not None:
            os.environ["DATABASE_URL"] = self._previous_database_url
        self.temp_dir.cleanup()

    # --- get_meeting_analysis ---

    def test_get_meeting_analysis_returns_stored_summary_without_reanalyzing(self) -> None:
        meeting_repository().create(__import__("app.domain.meeting", fromlist=["MeetingRecord"]).MeetingRecord("m-1", "user-a", "스프린트 리뷰"))
        meeting_repository().set_summary("m-1", "user-a", "다음 빌드 일정에 합의했다.")
        meeting_repository().upsert_action("a-1", "m-1", "user-a", "QA 결과 공유", "오늘 오후까지 공유")

        result = asyncio.run(get_meeting_analysis_workflow(_request("get_meeting_analysis", {"meeting_id": "m-1"})))

        self.assertFalse(result.mock)
        self.assertEqual(result.data["type"], "meeting_analysis")
        self.assertEqual(result.data["data"]["summary"], "다음 빌드 일정에 합의했다.")
        self.assertEqual(len(result.data["data"]["action_items"]), 1)
        self.assertIn("QA 결과 공유", result.markdown)

    def test_get_meeting_analysis_missing_meeting_id_is_a_value_error(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(get_meeting_analysis_workflow(_request("get_meeting_analysis", {})))

    def test_get_meeting_analysis_unanalyzed_meeting_raises_http_404(self) -> None:
        from fastapi import HTTPException

        meeting_repository().create(__import__("app.domain.meeting", fromlist=["MeetingRecord"]).MeetingRecord("m-2", "user-a", "아직 미분석"))
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(get_meeting_analysis_workflow(_request("get_meeting_analysis", {"meeting_id": "m-2"})))
        self.assertEqual(ctx.exception.status_code, 404)

    # --- review_action_items ---

    def test_review_action_items_approves_and_rejects_in_one_call(self) -> None:
        meeting_repository().create(__import__("app.domain.meeting", fromlist=["MeetingRecord"]).MeetingRecord("m-3", "user-a", "배치 검토"))
        meeting_repository().upsert_action("a-approve", "m-3", "user-a", "승인될 항목", "근거1")
        meeting_repository().upsert_action("a-reject", "m-3", "user-a", "거절될 항목", "근거2")

        result = asyncio.run(
            review_action_items_workflow(
                _request(
                    "review_action_items",
                    {
                        "meeting_id": "m-3",
                        "decisions": [
                            {"action_item_id": "a-approve", "decision": "approve"},
                            {"action_item_id": "a-reject", "decision": "reject"},
                        ],
                    },
                )
            )
        )

        results = {item["action_item_id"]: item for item in result.data["data"]["results"]}
        self.assertEqual(results["a-approve"]["approval_status"], "approved")
        self.assertEqual(results["a-reject"]["approval_status"], "rejected")

    def test_review_action_items_rejects_empty_decisions(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(review_action_items_workflow(_request("review_action_items", {"meeting_id": "m-3", "decisions": []})))

    # --- review_proposal ---

    def test_review_proposal_approves_an_email_proposal_into_a_task(self) -> None:
        asyncio.run(publish_email_proposal("user-a", GmailMessage("msg-1", "thread-1", "회신 요청", ("INBOX",)), title="메일 회신"))

        result = asyncio.run(
            review_proposal_workflow(
                _request(
                    "review_proposal",
                    {
                        "source_type": "email",
                        "decision": "approve",
                        "message_id": "msg-1",
                        "task": {"title": "메일 회신", "assignee_user_id": "user-a"},
                    },
                )
            )
        )

        self.assertEqual(result.data["data"]["decision"], "approve")
        self.assertTrue(result.data["data"]["created"])
        tasks = task_repository().list("user-a")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].source_id, "msg-1")

    def test_review_proposal_ignores_a_calendar_proposal_without_creating_a_task(self) -> None:
        asyncio.run(publish_calendar_proposal("user-a", CalendarEvent("primary", "evt-1", "빌드 회의", "confirmed", None, None, None)))

        result = asyncio.run(
            review_proposal_workflow(
                _request("review_proposal", {"source_type": "calendar", "decision": "ignore", "calendar_id": "primary", "event_id": "evt-1"})
            )
        )

        self.assertEqual(result.data["data"]["decision"], "ignore")
        self.assertEqual(task_repository().list("user-a"), [])

    def test_review_proposal_invalid_source_type_is_a_value_error(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(review_proposal_workflow(_request("review_proposal", {"source_type": "sms", "decision": "approve"})))

    # --- manage_tasks ---

    def test_manage_tasks_create_then_list_then_update_then_delete(self) -> None:
        created = asyncio.run(manage_tasks_workflow(_request("manage_tasks", {"action": "create", "title": "QA 리포트 작성"})))
        task_id = created.data["data"]["task_id"]
        self.assertEqual(created.data["data"]["title"], "QA 리포트 작성")

        listed = asyncio.run(manage_tasks_workflow(_request("manage_tasks", {"action": "list"})))
        self.assertEqual([item["task_id"] for item in listed.data["data"]["tasks"]], [task_id])

        updated = asyncio.run(manage_tasks_workflow(_request("manage_tasks", {"action": "update", "task_id": task_id, "status": "done"})))
        self.assertEqual(updated.data["data"]["status"], "done")

        deleted = asyncio.run(manage_tasks_workflow(_request("manage_tasks", {"action": "delete", "task_id": task_id})))
        self.assertEqual(deleted.data["data"]["task_id"], task_id)
        remaining = asyncio.run(manage_tasks_workflow(_request("manage_tasks", {"action": "list"})))
        self.assertEqual(remaining.data["data"]["tasks"], [])

    def test_manage_tasks_unsupported_action_is_a_value_error(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(manage_tasks_workflow(_request("manage_tasks", {"action": "archive"})))

    def test_manage_tasks_create_requires_a_title(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(manage_tasks_workflow(_request("manage_tasks", {"action": "create"})))

    # --- read_email (2026-08-17, 실사용 중 발견 — 제안 Context에는 제목뿐이라
    # "메일 내용 요약해줘"에 답할 근거가 없었다. 매 질문마다 Gmail을 다시
    # 조회해 실제 본문을 가져오는 이 Skill로 해결한다.) ---

    def test_read_email_default_credential_dir_matches_dev_gmail_sync_api(self) -> None:
        """실사용 중 발견한 회귀 — `assistant_skills.py`는 `app/workflows/` 아래에
        있어 `app/` 바로 아래인 `dev_gmail_sync_api.py`보다 한 단계 더 깊은데,
        기본 경로 계산이 그 깊이 차이를 반영하지 못해 같은 `google-oauth-test`
        디렉터리를 가리키지 못했다 — `GOOGLE_TOKEN_FILE`을 따로 지정하지 않은
        실제 서버에서 Token 파일이 있는데도 "Gmail 인증이 안 되어 있다"고
        잘못 답했다. 두 모듈의 기본 경로가 실제로 같은 디렉터리를 가리키는지
        확인한다(문자열이 아니라 값이 같아야 한다 — `dev_gmail_sync_api.py`가
        먼저 검증된 쪽이라 그쪽에 맞춘다)."""

        from app.dev_gmail_sync_api import _DEFAULT_OAUTH_TEST_DIR
        from app.workflows.assistant_skills import _GMAIL_OAUTH_TEST_DIR

        self.assertEqual(_GMAIL_OAUTH_TEST_DIR, _DEFAULT_OAUTH_TEST_DIR)

    def test_read_email_returns_no_token_message_when_credentials_are_missing(self) -> None:
        missing_path = os.path.join(self.temp_dir.name, "does-not-exist.json")
        with unittest.mock.patch.dict(os.environ, {"GOOGLE_TOKEN_FILE": missing_path}):
            result = asyncio.run(read_email_workflow(_request("read_email", {"message_id": "msg-1"})))
        self.assertFalse(result.mock)
        self.assertEqual(result.data["type"], "email_content")
        self.assertFalse(result.data["data"]["available"])
        self.assertIn("인증", result.data["data"]["reason"])

    def test_read_email_fetches_the_live_body_when_credentials_are_present(self) -> None:
        token_path = os.path.join(self.temp_dir.name, "token.json")
        with open(token_path, "w", encoding="utf-8") as handle:
            handle.write("{}")
        message = GmailMessage("msg-1", "thread-1", "짧은 미리보기", (), "시즌 패스 보상 지급 로직 변경")
        with unittest.mock.patch.dict(os.environ, {"GOOGLE_TOKEN_FILE": token_path}), unittest.mock.patch(
            "app.providers.google_auth.build_authorized_session", return_value=object()
        ), unittest.mock.patch(
            "app.providers.google.GmailAdapter.get_message_with_body",
            return_value=(message, "본문: v1.4.0 변경 내용을 클라이언트 연동에 반영해야 합니다."),
        ):
            result = asyncio.run(read_email_workflow(_request("read_email", {"message_id": "msg-1"})))
        self.assertFalse(result.mock)
        self.assertEqual(result.data["type"], "email_content")
        self.assertTrue(result.data["data"]["available"])
        self.assertEqual(result.data["data"]["subject"], "시즌 패스 보상 지급 로직 변경")
        self.assertIn("v1.4.0 변경 내용을 클라이언트 연동에 반영해야 합니다", result.data["data"]["body"])

    def test_read_email_strips_the_action_item_index_suffix_from_a_proposal_source_id(self) -> None:
        """실사용 중 발견한 버그(2026-08-17)의 회귀 테스트 — Context.proposals의
        message_id는 실제로 email 제안의 source_id(`f"{gmail_id}:{index}"`,
        `dev_gmail_sync_api.py`의 `_publish_action_items_for_message` 참고)다.
        Router가 이 값을 "그대로" Gmail Message ID로 넘기면 Gmail이 "Invalid
        id value" 400을 냈다. `:index` 접미사를 잘라내고 순수 Gmail Message
        ID로 호출하는지 확인한다."""

        token_path = os.path.join(self.temp_dir.name, "token.json")
        with open(token_path, "w", encoding="utf-8") as handle:
            handle.write("{}")
        message = GmailMessage("1a00ec3a449ec13e", "thread-1", "짧은 미리보기", (), "시즌 패스 보상 지급 로직 변경")
        with unittest.mock.patch.dict(os.environ, {"GOOGLE_TOKEN_FILE": token_path}), unittest.mock.patch(
            "app.providers.google_auth.build_authorized_session", return_value=object()
        ), unittest.mock.patch(
            "app.providers.google.GmailAdapter.get_message_with_body", return_value=(message, "본문")
        ) as get_with_body:
            result = asyncio.run(read_email_workflow(_request("read_email", {"message_id": "1a00ec3a449ec13e:0"})))

        get_with_body.assert_called_once_with("1a00ec3a449ec13e")
        self.assertTrue(result.data["data"]["available"])

    def test_read_email_requires_message_id_or_query(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(read_email_workflow(_request("read_email", {})))

    def test_read_email_searches_by_query_within_the_last_30_days_when_no_message_id_is_given(self) -> None:
        """실사용 중 발견한 요청(2026-08-17) — 제안함에 없는(이미 처리됐거나
        애초에 제안으로 뜬 적 없는) 메일도 제목·키워드로 메일함 전체에서
        찾아 읽을 수 있어야 한다. 단, 검색 범위는 최근 30일로 제한하고 그
        사실을 결과에 항상 실어야 한다."""

        token_path = os.path.join(self.temp_dir.name, "token.json")
        with open(token_path, "w", encoding="utf-8") as handle:
            handle.write("{}")
        found = GmailMessage("msg-found", "thread-1", "짧은 미리보기", ())
        message = GmailMessage("msg-found", "thread-1", "짧은 미리보기", (), "시즌 패스 보상 지급 로직 변경")
        with unittest.mock.patch.dict(os.environ, {"GOOGLE_TOKEN_FILE": token_path}), unittest.mock.patch(
            "app.providers.google_auth.build_authorized_session", return_value=object()
        ), unittest.mock.patch(
            "app.providers.google.GmailAdapter.list_messages", return_value=[found]
        ) as list_messages, unittest.mock.patch(
            "app.providers.google.GmailAdapter.get_message_with_body",
            return_value=(message, "본문: 시즌 패스 보상 지급 로직을 변경합니다."),
        ):
            result = asyncio.run(read_email_workflow(_request("read_email", {"query": "시즌 패스 보상 지급"})))

        list_messages.assert_called_once_with(query="시즌 패스 보상 지급 newer_than:30d", max_results=5)
        self.assertTrue(result.data["data"]["available"])
        self.assertEqual(result.data["data"]["message_id"], "msg-found")
        self.assertEqual(result.data["data"]["search_window_note"], "최근 30일 내 메일만 검색했습니다.")
        self.assertIn("최근 30일", result.text)

    def test_read_email_sanitizes_colons_and_quotes_out_of_the_search_query(self) -> None:
        """실사용 중 발견한 버그(2026-08-17)의 회귀 테스트 — Router가 뽑은 검색어에
        우연히 ":"·'"'가 섞이면(예: '제목: "시즌 패스"') Gmail이 이를
        `subject:`류 연산자·구문 검색으로 잘못 해석해 400을 내고 "Google
        provider 요청 실패"로만 답했다. 두 문자를 검색어에서 제거하는지
        확인한다."""

        token_path = os.path.join(self.temp_dir.name, "token.json")
        with open(token_path, "w", encoding="utf-8") as handle:
            handle.write("{}")
        found = GmailMessage("msg-found", "thread-1", "짧은 미리보기", ())
        message = GmailMessage("msg-found", "thread-1", "짧은 미리보기", (), "시즌 패스")
        with unittest.mock.patch.dict(os.environ, {"GOOGLE_TOKEN_FILE": token_path}), unittest.mock.patch(
            "app.providers.google_auth.build_authorized_session", return_value=object()
        ), unittest.mock.patch(
            "app.providers.google.GmailAdapter.list_messages", return_value=[found]
        ) as list_messages, unittest.mock.patch(
            "app.providers.google.GmailAdapter.get_message_with_body", return_value=(message, "본문")
        ):
            asyncio.run(read_email_workflow(_request("read_email", {"query": '제목: "시즌 패스"'})))

        list_messages.assert_called_once_with(query="제목   시즌 패스 newer_than:30d", max_results=5)

    def test_read_email_query_search_reports_when_nothing_is_found_within_30_days(self) -> None:
        token_path = os.path.join(self.temp_dir.name, "token.json")
        with open(token_path, "w", encoding="utf-8") as handle:
            handle.write("{}")
        with unittest.mock.patch.dict(os.environ, {"GOOGLE_TOKEN_FILE": token_path}), unittest.mock.patch(
            "app.providers.google_auth.build_authorized_session", return_value=object()
        ), unittest.mock.patch("app.providers.google.GmailAdapter.list_messages", return_value=[]):
            result = asyncio.run(read_email_workflow(_request("read_email", {"query": "존재하지 않는 메일"})))

        self.assertFalse(result.data["data"]["available"])
        self.assertIn("존재하지 않는 메일", result.data["data"]["reason"])
        self.assertEqual(result.data["data"]["search_window_note"], "최근 30일 내 메일만 검색했습니다.")
        self.assertIn("최근 30일", result.text)


if __name__ == "__main__":
    unittest.main()
