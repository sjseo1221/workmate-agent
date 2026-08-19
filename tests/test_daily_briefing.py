"""M1.5-04 일일 브리핑 Workflow 검증."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator, FormatChecker

from app.domain.task import TaskRecord
from app.email_action_items import EmailRelevanceProviderError
from app.providers.google import CalendarEvent, GmailMessage
from app.repositories.sync_state import SQLiteSyncStateRepository
from app.repositories.tasks import SQLiteTaskRepository
from app.workflows.daily_briefing import build_daily_briefing_workflow
from app.workflows.registry import WorkflowRequest

# 이 Workflow는 메일 신호를 `classify_work_related_emails`(실제 LLM 호출)로
# 거른다 — 대부분의 테스트는 그 판단 자체가 아니라 나머지 브리핑 조립을
# 검증하므로, 개발 쉘에 실제 `OPENAI_API_KEY`가 있어도 매 테스트가 진짜
# 네트워크를 타지 않도록 "모두 업무 관련"으로 기본 Patch한다. 필터링 자체를
# 검증하는 테스트는 이 기본값을 각자 다시 Patch한다.
_ALL_WORK_RELATED = patch(
    "app.workflows.daily_briefing.classify_work_related_emails",
    lambda messages, **kwargs: {message_id: True for message_id, _ in messages},
)


class _Gmail:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.last_query: str | None = None
        self.last_max_results: int | None = None

    def list_messages(self, **kwargs):
        self.last_query = kwargs.get("query")
        self.last_max_results = kwargs.get("max_results")
        if self.fail:
            raise RuntimeError("gmail unavailable")
        return [GmailMessage("message-1", "thread-1", "검토 요청", ("INBOX",))]


class _Calendar:
    def list_events(self, **kwargs):
        return [CalendarEvent("primary", "event-1", "팀 회의", "confirmed", "2026-08-13T09:00:00+09:00", "2026-08-13T10:00:00+09:00", None)], "next-token"


class _GmailWithDetail:
    """`list_messages`는 실제 Gmail API처럼 Snippet 없이 반환하지만,
    `get_message`로 개별 조회하면 제목·Snippet을 함께 준다."""

    def list_messages(self, **kwargs):
        return [GmailMessage("message-1", "thread-1", None, ("INBOX",))]

    def get_message(self, message_id: str):
        assert message_id == "message-1"
        return GmailMessage("message-1", "thread-1", "다음 주 일정 조율 부탁드립니다.", ("INBOX",), "신규 게임 기획 회의 일정 안내")


class DailyBriefingWorkflowTests(unittest.TestCase):
    """실제 Adapter 경계, 부분 실패와 승인 Schema를 검증한다."""

    def setUp(self) -> None:
        self._work_related_patch = _ALL_WORK_RELATED
        self._work_related_patch.start()
        self.addCleanup(self._work_related_patch.stop)

    def _run(self, *, gmail_fail: bool = False):
        directory = tempfile.TemporaryDirectory()
        path = str(Path(directory.name) / "workmate.sqlite3")
        os.environ["WORKMATE_TASK_DB_PATH"] = path
        tasks = SQLiteTaskRepository(path)
        tasks.create(TaskRecord(task_id="task-1", assignee_user_id="alice", title="보고서", priority_hint=8))
        sync = SQLiteSyncStateRepository(path)
        workflow = build_daily_briefing_workflow(
            provider_factory=lambda: (_Gmail(gmail_fail), _Calendar()),
            repositories=(tasks, sync),
        )
        result = asyncio.run(workflow(WorkflowRequest(
            "daily_briefing", "task-run", "thread-run", "message-run", "alice",
            {"user_id": "alice", "timezone": "Asia/Seoul", "as_of": "2026-08-13T08:00:00+09:00"},
        )))
        return directory, result

    def test_live_provider_data_and_task_top3_validate(self) -> None:
        directory, result = self._run()
        try:
            self.assertFalse(result.mock)
            payload = json.loads(result.text)
            self.assertEqual(payload["type"], "daily_briefing")
            self.assertEqual(payload["data"]["calendar_events"][0]["event_id"], "primary:event-1")
            self.assertEqual(payload["data"]["important_signals"][0]["source_id"], "message-1")
            self.assertEqual(result.warnings, [])
            self._validate_envelope(payload)
            # `WorkflowResult.data`는 `internal_chat.py`가 그대로 HTTP 응답의
            # `artifact.data`로 내보내는 필드다 — `text`(JSON 문자열)만 채우고 이
            # 필드를 비워두면 프론트(`useSkillRunner`)가 읽는
            # `response.artifact.data.data`는 항상 `null`이 되어, 요청이 200으로
            # 성공해도 화면은 로딩·오류 없이 "아직 실행 안 함"에 영원히 멈춘다
            # (2026-08-16, 14번 갭 문서 — 오늘 브리핑 실사용 중 재현·확인된 버그).
            self.assertEqual(result.data, payload)
        finally:
            directory.cleanup()

    def test_already_registered_calendar_event_and_email_show_related_task_ids(self) -> None:
        """이미 Task로 등록한 회의·메일은 `related_task_ids`가 채워져야 한다
        — 2026-08-15 이전엔 Calendar는 항상 빈 배열, Gmail은 필드 자체가
        없어 이미 등록한 항목도 매번 "아직 안 한 일"처럼 보였다(#32)."""

        directory, result = self._run()
        try:
            payload = json.loads(result.text)
            event = payload["data"]["calendar_events"][0]
            signal = payload["data"]["important_signals"][0]
            self.assertEqual(event["related_task_ids"], [])
            self.assertEqual(signal["related_task_ids"], [])
        finally:
            directory.cleanup()

        directory = tempfile.TemporaryDirectory()
        try:
            path = str(Path(directory.name) / "workmate.sqlite3")
            os.environ["WORKMATE_TASK_DB_PATH"] = path
            tasks = SQLiteTaskRepository(path)
            calendar_task = tasks.create(
                TaskRecord(task_id="task-cal", assignee_user_id="alice", title="팀 회의 후속", source_type="calendar", source_id="primary:event-1")
            )
            email_task = tasks.create(
                TaskRecord(task_id="task-mail", assignee_user_id="alice", title="검토 회신", source_type="email", source_id="message-1")
            )
            sync = SQLiteSyncStateRepository(path)
            workflow = build_daily_briefing_workflow(
                provider_factory=lambda: (_Gmail(), _Calendar()),
                repositories=(tasks, sync),
            )
            result = asyncio.run(
                workflow(
                    WorkflowRequest(
                        "daily_briefing", "task-run", "thread-run", "message-run", "alice",
                        {"user_id": "alice", "timezone": "Asia/Seoul", "as_of": "2026-08-13T08:00:00+09:00"},
                    )
                )
            )
            payload = json.loads(result.text)
            event = payload["data"]["calendar_events"][0]
            signal = payload["data"]["important_signals"][0]
            self.assertEqual(event["related_task_ids"], [calendar_task.task_id])
            self.assertEqual(signal["related_task_ids"], [email_task.task_id])
        finally:
            directory.cleanup()

    def test_provider_failure_returns_partial_result_and_warning(self) -> None:
        directory, result = self._run(gmail_fail=True)
        try:
            payload = json.loads(result.text)
            self.assertEqual(len(payload["data"]["calendar_events"]), 1)
            self.assertEqual(payload["data"]["important_signals"], [])
            self.assertEqual(result.warnings[0]["source"], "email")
            self.assertEqual(result.warnings[0]["code"], "GOOGLE_PROVIDER_UNAVAILABLE")
        finally:
            directory.cleanup()

    def test_gmail_query_excludes_promotions_social_and_updates_categories_and_caps_at_5(self) -> None:
        """`is:unread`만 쓰면 사람이 보낸 업무 메일과 구글 마케팅 알림(예:
        "최근 검색에 표시됨" 뉴스레터)이 구분 없이 섞여 "중요 업무 신호"에
        나온다(2026-08-16, 14번 갭 문서 — 실사용 중 발견). Gmail이 스스로
        분류하는 `category:primary`로 그런 메일을 제외해야 한다. `messages.list`는
        정렬 조건을 안 받지만 기본 결과 순서 자체가 최신 수신순이라, 개수를
        5건으로 제한하면 곧 최신 5건이 된다(2026-08-17, 사용자 요청 — 10건도
        여전히 많아 축소)."""

        directory = tempfile.TemporaryDirectory()
        try:
            path = str(Path(directory.name) / "workmate.sqlite3")
            os.environ["WORKMATE_TASK_DB_PATH"] = path
            tasks = SQLiteTaskRepository(path)
            sync = SQLiteSyncStateRepository(path)
            gmail = _Gmail()
            workflow = build_daily_briefing_workflow(
                provider_factory=lambda: (gmail, _Calendar()),
                repositories=(tasks, sync),
            )
            asyncio.run(
                workflow(
                    WorkflowRequest(
                        "daily_briefing", "task-run", "thread-run", "message-run", "alice",
                        {"user_id": "alice", "timezone": "Asia/Seoul", "as_of": "2026-08-13T08:00:00+09:00"},
                    )
                )
            )
            self.assertEqual(gmail.last_query, "is:unread category:primary")
            self.assertEqual(gmail.last_max_results, 5)
        finally:
            directory.cleanup()

    def test_important_signals_use_real_subject_and_snippet_not_a_placeholder(self) -> None:
        """`list_messages`(Gmail `users.messages.list`)는 Snippet·제목을 주지
        않는다 — 그대로 쓰면 메일이 몇 건이든 전부 "읽지 않은 메일 신호"라는
        똑같은 문구로만 보인다(2026-08-16, 14번 갭 문서 — 실사용 중 100건이
        전부 이 문구로만 보여 발견). 개별 조회(`get_message`)로 얻은 실제
        제목·Snippet을 써야 한다."""

        directory = tempfile.TemporaryDirectory()
        try:
            path = str(Path(directory.name) / "workmate.sqlite3")
            os.environ["WORKMATE_TASK_DB_PATH"] = path
            tasks = SQLiteTaskRepository(path)
            sync = SQLiteSyncStateRepository(path)
            workflow = build_daily_briefing_workflow(
                provider_factory=lambda: (_GmailWithDetail(), _Calendar()),
                repositories=(tasks, sync),
            )
            result = asyncio.run(
                workflow(
                    WorkflowRequest(
                        "daily_briefing", "task-run", "thread-run", "message-run", "alice",
                        {"user_id": "alice", "timezone": "Asia/Seoul", "as_of": "2026-08-13T08:00:00+09:00"},
                    )
                )
            )
            self.assertEqual(result.warnings, [])
            signal = json.loads(result.text)["data"]["important_signals"][0]
            self.assertEqual(signal["summary"], "신규 게임 기획 회의 일정 안내 — 다음 주 일정 조율 부탁드립니다.")
        finally:
            directory.cleanup()

    def test_llm_judged_non_work_email_is_skipped_from_important_signals(self) -> None:
        """LLM이 업무 무관(광고·뉴스레터 등)으로 판단한 메일은 "중요 업무
        신호"에서 빠져야 한다 — Gmail의 `category:primary`만으로는 사람이
        보낸 업무 메일과 Primary로 분류된 광고성 메일이 구분되지 않는다
        (2026-08-17, 사용자 요청)."""

        with patch(
            "app.workflows.daily_briefing.classify_work_related_emails",
            return_value={"message-1": False},
        ):
            directory, result = self._run()
        try:
            payload = json.loads(result.text)
            self.assertEqual(payload["data"]["important_signals"], [])
            self.assertNotIn("message-1", payload["data"]["source_refs"])
            self.assertEqual(result.warnings, [])
        finally:
            directory.cleanup()

    def test_relevance_classification_failure_fails_open_and_keeps_signal(self) -> None:
        """업무 관련성 판단(LLM) 자체가 실패해도(Provider 오류 등) 이미 가져온
        메일 신호를 조용히 숨기지 않는다 — 이 Workflow가 부분 실패를 허용하는
        원칙(예: Calendar·Gmail Provider 실패)을 그대로 따른다."""

        with patch(
            "app.workflows.daily_briefing.classify_work_related_emails",
            side_effect=EmailRelevanceProviderError("provider down"),
        ):
            directory, result = self._run()
        try:
            payload = json.loads(result.text)
            self.assertEqual(payload["data"]["important_signals"][0]["source_id"], "message-1")
            self.assertEqual(result.warnings, [])
        finally:
            directory.cleanup()

    @staticmethod
    def _validate_envelope(payload: dict[str, object]) -> None:
        schema_path = Path(__file__).resolve().parents[2] / "docs" / "schemas" / "workmate-skill-schemas.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator(schema, format_checker=FormatChecker()).validate({
            "schema_version": "1.0",
            "request_id": "request-1",
            "generated_at": payload["data"]["date"] + "T00:00:00Z",
            "data_freshness": {"task": None, "calendar": None, "email": None, "meeting": None},
            "warnings": [],
            "result": payload,
        })


class GoogleSessionTests(unittest.TestCase):
    """`_session()`이 Refresh Token으로 만료된 토큰을 자동 갱신하는지 검증한다.

    이전엔 `WORKMATE_GOOGLE_ACCESS_TOKEN` 단기 토큰만 썼는데 Refresh 정보가
    없어 약 1시간마다 수동으로 새 토큰을 발급하고 서버를 재기동해야 했다
    (2026-08-16 실사용 중 발견). 이제 `GOOGLE_TOKEN_FILE`의 Refresh Token으로
    자동 갱신한다."""

    def setUp(self) -> None:
        from app.repositories.google_credentials import google_credential_repository

        self.directory = tempfile.TemporaryDirectory()
        self.token_path = Path(self.directory.name) / "token.json"
        self._previous_token_file = os.environ.get("GOOGLE_TOKEN_FILE")
        self._previous_access_token = os.environ.get("WORKMATE_GOOGLE_ACCESS_TOKEN")
        self._previous_credential_db_path = os.environ.get("WORKMATE_GOOGLE_CREDENTIAL_DB_PATH")
        os.environ["GOOGLE_TOKEN_FILE"] = str(self.token_path)
        # `_session()`이 공유 파일보다 먼저 확인하는 사용자별 연결 저장소를
        # 실제 `.runtime/google_credentials.sqlite3`가 아니라 이 테스트 전용
        # 빈 DB로 격리한다 — 그래야 이 클래스의 테스트들이 원래 검증하려던
        # 공유 파일/환경변수 폴백 경로를 그대로 재현한다.
        os.environ["WORKMATE_GOOGLE_CREDENTIAL_DB_PATH"] = str(Path(self.directory.name) / "google_credentials.sqlite3")
        google_credential_repository.cache_clear()

    def tearDown(self) -> None:
        from app.repositories.google_credentials import google_credential_repository

        for name, previous in (
            ("GOOGLE_TOKEN_FILE", self._previous_token_file),
            ("WORKMATE_GOOGLE_ACCESS_TOKEN", self._previous_access_token),
            ("WORKMATE_GOOGLE_CREDENTIAL_DB_PATH", self._previous_credential_db_path),
        ):
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
        google_credential_repository.cache_clear()
        self.directory.cleanup()

    def _write_token(self, *, expired: bool) -> None:
        expiry = "2000-01-01T00:00:00Z" if expired else "2999-01-01T00:00:00Z"
        self.token_path.write_text(json.dumps({
            "token": "old-access-token",
            "refresh_token": "refresh-token-value",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id-value",
            "client_secret": "client-secret-value",
            "scopes": [
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/calendar.readonly",
            ],
            "expiry": expiry,
        }), encoding="utf-8")

    def test_fresh_token_file_is_still_force_refreshed_before_use(self) -> None:
        """저장된 만료 시각이 아직 안 지났어도(`expired=False`) 항상 갱신한다.

        `credentials.expired`는 저장된 만료 시각만 볼 뿐 Access Token이
        서버에서 실제로 아직 유효한지 확인하지 않는다 — 이 세션에서 그
        가정이 두 번 깨져 Calendar 403("Google authorization required")이
        반복됐다(2026-08-17, `app/providers/google_auth.py` 근본 수정).
        Refresh Token이 있으면 `expired` 값과 무관하게 매번 갱신해야
        한다."""

        from unittest.mock import patch

        from app.workflows.daily_briefing import _session

        self._write_token(expired=False)

        def _fake_refresh(self, request):  # noqa: ANN001 - google-auth 시그니처를 그대로 흉내낸다.
            self.token = "refreshed-access-token"

        with patch("google.oauth2.credentials.Credentials.refresh", _fake_refresh):
            session = _session("no-such-user")
        self.assertEqual(session.credentials.token, "refreshed-access-token")
        persisted = json.loads(self.token_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["token"], "refreshed-access-token")

    def test_expired_token_is_refreshed_and_persisted(self) -> None:
        from unittest.mock import patch

        from app.workflows.daily_briefing import _session

        self._write_token(expired=True)

        def _fake_refresh(self, request):  # noqa: ANN001 - google-auth 시그니처를 그대로 흉내낸다.
            from datetime import datetime, timedelta, timezone

            self.token = "refreshed-access-token"
            # google-auth는 naive UTC datetime을 쓴다(timezone-aware가 아님).
            self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

        with patch("google.oauth2.credentials.Credentials.refresh", _fake_refresh):
            session = _session("no-such-user")
        self.assertEqual(session.credentials.token, "refreshed-access-token")
        persisted = json.loads(self.token_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["token"], "refreshed-access-token")

    def test_connected_user_credential_is_preferred_over_shared_token_file(self) -> None:
        """"제안함"에서 개별 Google 계정을 연결해 둔 사용자는 공유
        `token.json`이 없어도(또는 만료돼 있어도) "오늘 브리핑"이 그 개별
        연결을 써야 한다 — 이 우선순위가 빠져 "제안함은 되는데 오늘
        브리핑은 안 된다"는 불일치가 났다(2026-08-19 실사용 중 발견)."""

        from unittest.mock import patch

        from app.repositories.google_credentials import google_credential_repository
        from app.workflows.daily_briefing import _session

        expiry = "2999-01-01T00:00:00Z"
        google_credential_repository().save(
            "alice",
            json.dumps({
                "token": "user-access-token",
                "refresh_token": "user-refresh-token",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "client-id-value",
                "client_secret": "client-secret-value",
                "scopes": [
                    "https://www.googleapis.com/auth/gmail.readonly",
                    "https://www.googleapis.com/auth/calendar.readonly",
                ],
                "expiry": expiry,
            }),
        )
        self.token_path.unlink(missing_ok=True)  # 공유 파일도, 환경변수도 없다
        os.environ.pop("WORKMATE_GOOGLE_ACCESS_TOKEN", None)

        def _fake_refresh(self, request):  # noqa: ANN001 - google-auth 시그니처를 그대로 흉내낸다.
            self.token = "refreshed-user-access-token"

        with patch("google.oauth2.credentials.Credentials.refresh", _fake_refresh):
            session = _session("alice")
        self.assertEqual(session.credentials.token, "refreshed-user-access-token")

    def test_missing_token_file_falls_back_to_bare_access_token(self) -> None:
        from app.workflows.daily_briefing import _session

        os.environ["WORKMATE_GOOGLE_ACCESS_TOKEN"] = "bare-token-value"
        self.token_path.unlink(missing_ok=True)
        session = _session("no-such-user")
        self.assertEqual(session.credentials.token, "bare-token-value")

    def test_missing_token_file_and_no_access_token_raises_configuration_error(self) -> None:
        from app.workflows.daily_briefing import ProviderConfigurationError, _session

        os.environ.pop("WORKMATE_GOOGLE_ACCESS_TOKEN", None)
        self.token_path.unlink(missing_ok=True)
        with self.assertRaises(ProviderConfigurationError):
            _session("no-such-user")

    def test_default_token_path_points_at_the_sibling_google_oauth_test_directory(self) -> None:
        """`app/workflows/daily_briefing.py`는 `app/dev_gmail_sync_api.py`보다 한
        단계 더 깊어(`app/workflows/`) `parents[2]`가 아니라 `parents[3]`이어야
        저장소 루트의 `google-oauth-test/`를 가리킨다 — 실사용 중 발견한 경로
        계산 버그(2026-08-17)."""

        from app.workflows.daily_briefing import _DEFAULT_OAUTH_TEST_DIR

        self.assertTrue((_DEFAULT_OAUTH_TEST_DIR / "auth_flow.py").is_file())


if __name__ == "__main__":
    unittest.main()
