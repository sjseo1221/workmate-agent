"""개발용 Calendar 동기화 트리거(`/api/v1/dev/calendar-sync` 등) 계약 테스트.

`tests/test_dev_gmail_sync_api.py`와 같은 방식으로 실제 Google 계정을 호출하지 않는다.
`_load_authorized_session`만 Fake로 바꿔 Adapter 이후의 발행 경로(필터·중복 검사·
proposal_hub 발행·응답 형태)를 검증한다.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.dev_calendar_sync_api import (
    CALENDAR_WATCH_ADDRESS_ENV,
    DEV_CALENDAR_SYNC_ENABLED_ENV,
    _channel_index,
)
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


def _events_response(items: list[dict], next_sync_token: str | None = None) -> FakeResponse:
    body: dict = {"items": items}
    if next_sync_token:
        body["nextSyncToken"] = next_sync_token
    return FakeResponse(200, body)


def _event(event_id: str, summary: str, *, status: str = "confirmed", start: str | None = None) -> dict:
    start = start or (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    return {
        "id": event_id,
        "summary": summary,
        "status": status,
        "start": {"dateTime": start},
        "end": {"dateTime": start},
        "htmlLink": f"https://calendar.google.com/event?eid={event_id}",
    }


class DevCalendarSyncApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        app.dependency_overrides[_authenticated_user_or_assignee] = lambda: "user-a"
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        app.dependency_overrides.pop(_authenticated_user_or_assignee, None)

    def setUp(self) -> None:
        # 매 테스트마다 독립된 SQLite 파일을 써서 `SyncStateRepository`의
        # upsert 상태(등록된 Watch 등)가 테스트 간에 새어나가지 않게 한다 —
        # `sync_state_repository()`는 `task_repository()`와 달리 캐시하지
        # 않고 매 호출마다 새로 만들지만, 같은 파일 경로를 쓰면 그 파일
        # 안의 행은 그대로 남는다.
        self.temp_dir = tempfile.TemporaryDirectory()
        self._previous_database_url = os.environ.pop("DATABASE_URL", None)
        self._previous_db_path = os.environ.get("WORKMATE_TASK_DB_PATH")
        self._previous_ignored_db_path = os.environ.get("WORKMATE_IGNORED_PROPOSALS_DB_PATH")
        os.environ["WORKMATE_TASK_DB_PATH"] = os.path.join(self.temp_dir.name, "tasks.sqlite3")
        os.environ["WORKMATE_IGNORED_PROPOSALS_DB_PATH"] = os.path.join(self.temp_dir.name, "ignored_proposals.sqlite3")
        task_repository.cache_clear()
        ignored_proposal_repository.cache_clear()
        proposal_hub.clear()
        _channel_index.clear()
        self._previous_flag = os.environ.get(DEV_CALENDAR_SYNC_ENABLED_ENV)
        self._previous_address = os.environ.get(CALENDAR_WATCH_ADDRESS_ENV)

    def tearDown(self) -> None:
        proposal_hub.clear()
        _channel_index.clear()
        for env_name, previous in (
            (DEV_CALENDAR_SYNC_ENABLED_ENV, self._previous_flag),
            (CALENDAR_WATCH_ADDRESS_ENV, self._previous_address),
        ):
            if previous is None:
                os.environ.pop(env_name, None)
            else:
                os.environ[env_name] = previous
        task_repository.cache_clear()
        ignored_proposal_repository.cache_clear()
        if self._previous_database_url is not None:
            os.environ["DATABASE_URL"] = self._previous_database_url
        if self._previous_db_path is None:
            os.environ.pop("WORKMATE_TASK_DB_PATH", None)
        else:
            os.environ["WORKMATE_TASK_DB_PATH"] = self._previous_db_path
        if self._previous_ignored_db_path is None:
            os.environ.pop("WORKMATE_IGNORED_PROPOSALS_DB_PATH", None)
        else:
            os.environ["WORKMATE_IGNORED_PROPOSALS_DB_PATH"] = self._previous_ignored_db_path
        self.temp_dir.cleanup()

    def test_disabled_by_default_returns_404(self) -> None:
        os.environ.pop(DEV_CALENDAR_SYNC_ENABLED_ENV, None)
        response = self.client.post("/api/v1/dev/calendar-sync")
        self.assertEqual(response.status_code, 404)

    def test_publishes_only_confirmed_future_events(self) -> None:
        os.environ[DEV_CALENDAR_SYNC_ENABLED_ENV] = "true"
        future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        fake_session = FakeSession([
            _events_response([
                _event("e-1", "빌드 회의", start=future),
                _event("e-2", "임시 일정", status="tentative", start=future),
                _event("e-3", "지난 회의", start=past),
                _event("e-4", "취소된 회의", status="cancelled", start=future),
            ]),
        ])
        with patch("app.dev_calendar_sync_api._load_authorized_session", return_value=fake_session):
            response = self.client.post("/api/v1/dev/calendar-sync")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["fetched"], 4)
        self.assertEqual(body["published"], [{"source_id": "primary:e-1", "title": "빌드 회의"}])
        reasons = {item["source_id"]: item["reason"] for item in body["skipped"]}
        self.assertEqual(reasons["primary:e-2"], "not_confirmed_or_out_of_range")
        self.assertEqual(reasons["primary:e-3"], "not_confirmed_or_out_of_range")
        self.assertEqual(reasons["primary:e-4"], "not_confirmed_or_out_of_range")
        self.assertIsNotNone(proposal_hub.get("user-a", "calendar", "primary:e-1"))

    def test_includes_events_earlier_today_and_excludes_yesterday(self) -> None:
        """"오늘 이후"는 로컬(Asia/Seoul) 자정 기준이다 — 이미 시작한 오늘 일정도
        포함하고 어제 일정만 제외한다(2026-08-17, 사용자 요청). 예전엔 `now`
        (이 순간) 기준이라 이미 지난 오늘 일정이 "지연 업무"와 똑같이 걸러졌다."""

        os.environ[DEV_CALENDAR_SYNC_ENABLED_ENV] = "true"
        from zoneinfo import ZoneInfo

        now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
        earlier_today = now_kst.replace(hour=0, minute=1, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        yesterday = (now_kst - timedelta(days=1)).astimezone(timezone.utc).isoformat()
        fake_session = FakeSession([
            _events_response([
                _event("e-today", "오늘 이른 시간 일정", start=earlier_today),
                _event("e-yesterday", "어제 일정", start=yesterday),
            ]),
        ])
        with patch("app.dev_calendar_sync_api._load_authorized_session", return_value=fake_session):
            response = self.client.post("/api/v1/dev/calendar-sync")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        published_ids = {item["source_id"] for item in body["published"]}
        self.assertIn("primary:e-today", published_ids)
        self.assertNotIn("primary:e-yesterday", published_ids)

    def test_excludes_events_more_than_a_month_away(self) -> None:
        """"오늘부터 한달"은 상한도 있다(2026-08-17, 사용자 요청 "오늘 이후" →
        "오늘부터 한달") — 두 달 뒤 일정은 걸러지고, 3주 뒤(한 달 안) 일정은
        그대로 제안된다."""

        os.environ[DEV_CALENDAR_SYNC_ENABLED_ENV] = "true"
        within_month = (datetime.now(timezone.utc) + timedelta(days=21)).isoformat()
        beyond_month = (datetime.now(timezone.utc) + timedelta(days=60)).isoformat()
        fake_session = FakeSession([
            _events_response([
                _event("e-within", "3주 뒤 일정", start=within_month),
                _event("e-beyond", "두 달 뒤 일정", start=beyond_month),
            ]),
        ])
        with patch("app.dev_calendar_sync_api._load_authorized_session", return_value=fake_session):
            response = self.client.post("/api/v1/dev/calendar-sync")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        published_ids = {item["source_id"] for item in body["published"]}
        skipped_ids = {item["source_id"]: item["reason"] for item in body["skipped"]}
        self.assertIn("primary:e-within", published_ids)
        self.assertEqual(skipped_ids.get("primary:e-beyond"), "not_confirmed_or_out_of_range")

    def test_published_proposal_carries_the_event_start_as_due_at(self) -> None:
        """`due_at`이 항상 `null`이던 문제를 고쳤다 — 이제 일정 시작 시각을 담아
        프론트가 "당일 것만 Toast" 필터를 적용할 수 있다(2026-08-17)."""

        os.environ[DEV_CALENDAR_SYNC_ENABLED_ENV] = "true"
        start = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        fake_session = FakeSession([_events_response([_event("e-due", "출시 점검", start=start)])])
        with patch("app.dev_calendar_sync_api._load_authorized_session", return_value=fake_session):
            response = self.client.post("/api/v1/dev/calendar-sync")
        self.assertEqual(response.status_code, 200)
        proposal = proposal_hub.get("user-a", "calendar", "primary:e-due")
        self.assertIsNotNone(proposal)
        self.assertIsNotNone(proposal.due_at)
        self.assertEqual(proposal.due_at.isoformat(), start)

    def test_skips_already_approved_event(self) -> None:
        os.environ[DEV_CALENDAR_SYNC_ENABLED_ENV] = "true"
        from app.domain.task import TaskRecord

        task_repository().create(
            TaskRecord(
                task_id="task-approved",
                assignee_user_id="user-a",
                title="이미 승인된 일정",
                source_type="calendar",
                source_id="primary:e-approved",
            )
        )
        fake_session = FakeSession([_events_response([_event("e-approved", "이미 승인된 일정")])])
        with patch("app.dev_calendar_sync_api._load_authorized_session", return_value=fake_session):
            response = self.client.post("/api/v1/dev/calendar-sync")
        body = response.json()
        self.assertEqual(body["published"], [])
        self.assertEqual(body["skipped"], [{"source_id": "primary:e-approved", "reason": "already_approved"}])

    def test_skips_already_ignored_event(self) -> None:
        """"무시"했던 일정도 승인된 일정과 같은 이유로 재동기화 시 다시
        제안하지 않는다(2026-08-16, 17번 갭 문서 #2) — 이전엔 이 기록 자체가
        없어 수동 동기화 버튼을 누를 때마다 이미 무시한 일정이 그대로 다시
        노출됐다."""

        os.environ[DEV_CALENDAR_SYNC_ENABLED_ENV] = "true"
        ignored_proposal_repository().mark("user-a", "calendar", "primary:e-ignored")
        fake_session = FakeSession([_events_response([_event("e-ignored", "이미 무시한 일정")])])
        with patch("app.dev_calendar_sync_api._load_authorized_session", return_value=fake_session):
            response = self.client.post("/api/v1/dev/calendar-sync")
        body = response.json()
        self.assertEqual(body["published"], [])
        self.assertEqual(body["skipped"], [{"source_id": "primary:e-ignored", "reason": "already_ignored"}])

    def test_ignoring_a_calendar_proposal_marks_it_so_future_syncs_skip_it(self) -> None:
        """"무시" 결정이 실제로 `ignored_proposal_repository`에 기록되는지
        Review 엔드포인트를 통해 확인한다."""

        import asyncio

        from app.proposal_api import TaskProposal

        proposal = TaskProposal(
            proposal_id="calendar-primary:e-review",
            user_id="user-a",
            source_type="calendar",
            source_id="primary:e-review",
            title="검토용 일정",
            assignee_user_id="user-a",
        )
        asyncio.run(proposal_hub.publish(proposal))
        response = self.client.post(
            "/api/v1/calendar-task-proposals:review",
            json={"calendar_id": "primary", "event_id": "e-review", "decision": "ignore"},
            headers={"Idempotency-Key": "test-ignore-calendar-key-1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("primary:e-review", ignored_proposal_repository().ignored_source_ids("user-a", "calendar"))

    def test_register_watch_requires_address_configuration(self) -> None:
        os.environ[DEV_CALENDAR_SYNC_ENABLED_ENV] = "true"
        os.environ.pop(CALENDAR_WATCH_ADDRESS_ENV, None)
        response = self.client.post("/api/v1/dev/calendar-watch/register")
        self.assertEqual(response.status_code, 503)

    def test_register_watch_persists_state_and_reports_status(self) -> None:
        os.environ[DEV_CALENDAR_SYNC_ENABLED_ENV] = "true"
        os.environ[CALENDAR_WATCH_ADDRESS_ENV] = "https://example.ngrok.app/webhooks/google-calendar"
        fake_session = FakeSession([FakeResponse(200, {"resourceId": "res-1", "expiration": "4102444800000"})])
        with patch("app.dev_calendar_sync_api._load_authorized_session", return_value=fake_session):
            response = self.client.post("/api/v1/dev/calendar-watch/register")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["resource_id"], "res-1")
        self.assertIsNotNone(body["channel_id"])
        self.assertIn(body["channel_id"], _channel_index)

        status_response = self.client.get("/api/v1/dev/calendar-watch/status")
        self.assertEqual(status_response.status_code, 200)
        status_body = status_response.json()
        self.assertTrue(status_body["registered"])
        self.assertEqual(status_body["resource_id"], "res-1")

    def test_watch_status_when_unregistered(self) -> None:
        os.environ[DEV_CALENDAR_SYNC_ENABLED_ENV] = "true"
        response = self.client.get("/api/v1/dev/calendar-watch/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"registered": False})

    def test_webhook_ignores_unknown_channel(self) -> None:
        os.environ[DEV_CALENDAR_SYNC_ENABLED_ENV] = "true"
        response = self.client.post(
            "/webhooks/google-calendar",
            headers={
                "X-Goog-Channel-Id": "unknown-channel",
                "X-Goog-Channel-Token": "token",
                "X-Goog-Resource-Id": "resource",
            },
        )
        self.assertEqual(response.status_code, 204)

    def test_webhook_triggers_background_sync_for_registered_channel(self) -> None:
        os.environ[DEV_CALENDAR_SYNC_ENABLED_ENV] = "true"
        os.environ[CALENDAR_WATCH_ADDRESS_ENV] = "https://example.ngrok.app/webhooks/google-calendar"
        register_session = FakeSession([FakeResponse(200, {"resourceId": "res-2", "expiration": "4102444800000"})])
        with patch("app.dev_calendar_sync_api._load_authorized_session", return_value=register_session):
            register_response = self.client.post("/api/v1/dev/calendar-watch/register")
        registered = register_response.json()
        # `channel_token`은 응답에 노출하지 않는 Secret이라 등록 결과에서 직접
        # 읽을 수 없다 — 실제 Webhook이 하듯 저장소에서 조회해 사용한다.
        from app.dev_calendar_sync_api import sync_state_repository

        stored = sync_state_repository().get("user-a", "google_calendar")
        assert stored is not None

        sync_session = FakeSession([_events_response([_event("e-webhook", "웹훅으로 온 일정")], next_sync_token="tok-2")])
        with patch("app.dev_calendar_sync_api._load_authorized_session", return_value=sync_session):
            response = self.client.post(
                "/webhooks/google-calendar",
                headers={
                    "X-Goog-Channel-Id": registered["channel_id"],
                    "X-Goog-Channel-Token": stored.channel_token,
                    "X-Goog-Resource-Id": registered["resource_id"],
                },
            )
        self.assertEqual(response.status_code, 204)
        # BackgroundTasks는 TestClient가 응답을 만든 뒤 동기적으로 실행하므로
        # 여기서 바로 발행 결과와 새 sync_cursor 저장을 확인할 수 있다.
        self.assertIsNotNone(proposal_hub.get("user-a", "calendar", "primary:e-webhook"))
        refreshed = sync_state_repository().get("user-a", "google_calendar")
        assert refreshed is not None
        self.assertEqual(refreshed.sync_cursor, "tok-2")

    def test_webhook_rejects_mismatched_token(self) -> None:
        os.environ[DEV_CALENDAR_SYNC_ENABLED_ENV] = "true"
        os.environ[CALENDAR_WATCH_ADDRESS_ENV] = "https://example.ngrok.app/webhooks/google-calendar"
        register_session = FakeSession([FakeResponse(200, {"resourceId": "res-3", "expiration": "4102444800000"})])
        with patch("app.dev_calendar_sync_api._load_authorized_session", return_value=register_session):
            register_response = self.client.post("/api/v1/dev/calendar-watch/register")
        registered = register_response.json()
        response = self.client.post(
            "/webhooks/google-calendar",
            headers={
                "X-Goog-Channel-Id": registered["channel_id"],
                "X-Goog-Channel-Token": "wrong-token",
                "X-Goog-Resource-Id": registered["resource_id"],
            },
        )
        # 검증 실패도 Google에는 조용히 204 — 재시도 폭주를 막기 위함(모듈 Docstring 참고).
        self.assertEqual(response.status_code, 204)
        self.assertIsNone(proposal_hub.get("user-a", "calendar", "primary:e-webhook"))


class AddOneMonthTests(unittest.TestCase):
    """`_add_one_month`의 월말·연말 경계를 검증한다."""

    def test_regular_day_moves_to_the_same_day_next_month(self) -> None:
        from datetime import date

        from app.dev_calendar_sync_api import _add_one_month

        self.assertEqual(_add_one_month(date(2026, 8, 17)), date(2026, 9, 17))

    def test_end_of_january_clamps_to_the_last_day_of_february(self) -> None:
        from datetime import date

        from app.dev_calendar_sync_api import _add_one_month

        self.assertEqual(_add_one_month(date(2026, 1, 31)), date(2026, 2, 28))

    def test_december_rolls_over_to_next_year(self) -> None:
        from datetime import date

        from app.dev_calendar_sync_api import _add_one_month

        self.assertEqual(_add_one_month(date(2026, 12, 15)), date(2027, 1, 15))


if __name__ == "__main__":
    unittest.main()
