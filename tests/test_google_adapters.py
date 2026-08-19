"""M1.2 Gmail·Calendar Adapter와 증분 동기화 Workflow 계약 테스트."""

from __future__ import annotations

import unittest

import base64

from app.providers.google import GmailAdapter, GoogleCalendarAdapter, GoogleProviderError, SyncCursorExpiredError
from app.domain.sync_state import SyncStateRecord
from app.repositories.sync_state import SQLiteSyncStateRepository
from app.workflows.external_sync import ExternalSyncWorkflow


class FakeResponse:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self.body = body

    def json(self) -> dict:
        return self.body


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = iter(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return next(self.responses)


class GoogleAdapterTests(unittest.TestCase):
    def test_gmail_message_normalization_and_profile(self) -> None:
        session = FakeSession([
            FakeResponse(200, {"historyId": "h-2"}),
            FakeResponse(200, {"messages": [{"id": "m-1", "threadId": "t-1", "snippet": "요청", "labelIds": ["INBOX"]}]}),
        ])
        adapter = GmailAdapter(session)
        self.assertEqual(adapter.profile()["historyId"], "h-2")
        message = adapter.list_messages()[0]
        self.assertEqual(message.message_id, "m-1")
        self.assertEqual(message.label_ids, ("INBOX",))

    def test_get_message_returns_snippet_and_subject_that_list_does_not_provide(self) -> None:
        """`messages.list`는 id·threadId만 주므로 개별 GET으로 Snippet·제목을 보강한다."""

        session = FakeSession([
            FakeResponse(200, {
                "id": "m-1",
                "threadId": "t-1",
                "snippet": "QA 빌드 배포 일정 확인 요청 드립니다...",
                "labelIds": ["INBOX"],
                "payload": {"headers": [{"name": "Subject", "value": "QA 빌드 배포 일정 확인 요청"}]},
            }),
        ])
        message = GmailAdapter(session).get_message("m-1")
        self.assertEqual(message.snippet, "QA 빌드 배포 일정 확인 요청 드립니다...")
        self.assertEqual(message.subject, "QA 빌드 배포 일정 확인 요청")
        self.assertEqual(message.label_ids, ("INBOX",))

    def test_get_message_subject_is_none_when_header_absent(self) -> None:
        session = FakeSession([FakeResponse(200, {"id": "m-2", "threadId": "t-2", "snippet": "", "payload": {"headers": []}})])
        message = GmailAdapter(session).get_message("m-2")
        self.assertIsNone(message.subject)

    def test_get_message_parses_internal_date_as_received_at(self) -> None:
        """`internalDate`(수신 시각, epoch 밀리초 문자열)를 ISO 8601 UTC로
        변환한다 — 제안함 카드의 수신 날짜 표시·정렬에 쓴다(2026-08-17)."""

        session = FakeSession([FakeResponse(200, {
            "id": "m-3", "threadId": "t-3", "snippet": "", "payload": {"headers": []},
            "internalDate": "1755000000000",
        })])
        message = GmailAdapter(session).get_message("m-3")
        self.assertEqual(message.received_at, "2025-08-12T12:00:00+00:00")

    def test_get_message_received_at_is_none_when_internal_date_missing_or_invalid(self) -> None:
        session = FakeSession([
            FakeResponse(200, {"id": "m-4", "threadId": "t-4", "snippet": "", "payload": {"headers": []}}),
            FakeResponse(200, {"id": "m-5", "threadId": "t-5", "snippet": "", "payload": {"headers": []}, "internalDate": "not-a-number"}),
        ])
        adapter = GmailAdapter(session)
        self.assertIsNone(adapter.get_message("m-4").received_at)
        self.assertIsNone(adapter.get_message("m-5").received_at)

    def test_get_message_with_body_prefers_plain_text_part(self) -> None:
        plain = base64.urlsafe_b64encode("본문 평문입니다".encode()).decode()
        session = FakeSession([
            FakeResponse(200, {
                "id": "m-1", "threadId": "t-1", "snippet": "짧은 미리보기",
                "payload": {
                    "headers": [{"name": "Subject", "value": "회의 일정 확인"}],
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": plain}},
                        {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b"<p>html</p>").decode()}},
                    ],
                },
            }),
        ])
        message, body = GmailAdapter(session).get_message_with_body("m-1")
        self.assertEqual(message.subject, "회의 일정 확인")
        self.assertEqual(body, "본문 평문입니다")

    def test_get_message_with_body_falls_back_to_stripped_html(self) -> None:
        html = "<div>안녕하세요<br/>확인 부탁드립니다. &#39;승인&#39; 눌러주세요.</div>"
        data = base64.urlsafe_b64encode(html.encode()).decode()
        session = FakeSession([
            FakeResponse(200, {
                "id": "m-2", "threadId": "t-2", "snippet": "미리보기",
                "payload": {"headers": [], "mimeType": "text/html", "body": {"data": data}},
            }),
        ])
        _, body = GmailAdapter(session).get_message_with_body("m-2")
        self.assertNotIn("<", body)
        self.assertIn("'승인'", body)

    def test_get_message_with_body_falls_back_to_snippet_when_no_parts(self) -> None:
        session = FakeSession([
            FakeResponse(200, {"id": "m-3", "threadId": "t-3", "snippet": "본문 없음 미리보기", "payload": {"headers": []}}),
        ])
        _, body = GmailAdapter(session).get_message_with_body("m-3")
        self.assertEqual(body, "본문 없음 미리보기")

    def test_calendar_event_source_id(self) -> None:
        session = FakeSession([FakeResponse(200, {"items": [{"id": "e-1", "summary": "회의", "start": {"dateTime": "2026-08-13T09:00:00Z"}}], "nextSyncToken": "c-2"})])
        events, token = GoogleCalendarAdapter(session, calendar_id="primary").list_events()
        self.assertEqual(token, "c-2")
        self.assertEqual(events[0].source_id, "primary:e-1")

    def test_http_410_is_cursor_expired(self) -> None:
        session = FakeSession([FakeResponse(410, {})])
        with self.assertRaises(SyncCursorExpiredError):
            GoogleCalendarAdapter(session).list_events(sync_token="expired")

    def test_generic_4xx_error_includes_googles_own_message(self) -> None:
        """실사용 중 발견한 진단 문제(2026-08-17) — 검색 쿼리 문법 오류 같은 400
        오류가 "Google provider request failed"라는 뭉뚱그린 문구만 남기고
        Google이 실제로 알려준 이유(예: 잘못된 검색 연산자)를 버려서 원인을
        재현·진단할 수 없었다. Google 응답의 `error.message`를 그대로 붙이는지
        확인한다."""

        session = FakeSession([FakeResponse(400, {"error": {"code": 400, "message": "Invalid search query: newer_than"}})])
        with self.assertRaises(GoogleProviderError) as ctx:
            GmailAdapter(session).list_messages(query="newer_than:bad-syntax")
        self.assertIn("Invalid search query: newer_than", str(ctx.exception))

    def test_generic_4xx_error_falls_back_to_status_code_when_google_gives_no_message(self) -> None:
        session = FakeSession([FakeResponse(400, {})])
        with self.assertRaises(GoogleProviderError) as ctx:
            GmailAdapter(session).list_messages()
        self.assertIn("HTTP 400", str(ctx.exception))

    def test_gmail_workflow_initializes_and_persists_cursor(self) -> None:
        session = FakeSession([
            FakeResponse(200, {"historyId": "h-1"}),
            FakeResponse(200, {"messages": [{"id": "m-1"}]}),
        ])
        repository = SQLiteSyncStateRepository()
        result = ExternalSyncWorkflow(repository).sync_gmail("user-a", GmailAdapter(session))
        self.assertEqual(result.next_cursor, "h-1")
        self.assertEqual(repository.get("user-a", "gmail").sync_cursor, "h-1")

    def test_calendar_workflow_resets_expired_cursor(self) -> None:
        session = FakeSession([
            FakeResponse(410, {}),
            FakeResponse(200, {"items": [{"id": "e-1", "summary": "회의"}], "nextSyncToken": "c-2"}),
        ])
        repository = SQLiteSyncStateRepository()
        repository.upsert(SyncStateRecord(
            source_sync_state_id="state-1", sync_user_id="user-a", source_type="google_calendar", sync_cursor="expired", status="succeeded"
        ))
        result = ExternalSyncWorkflow(repository).sync_calendar("user-a", GoogleCalendarAdapter(session))
        self.assertEqual(result.warnings, ("calendar_sync_cursor_expired_reset",))
        self.assertEqual(repository.get("user-a", "google_calendar").sync_cursor, "c-2")


if __name__ == "__main__":
    unittest.main()
