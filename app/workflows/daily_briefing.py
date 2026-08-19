"""M1.5-04 실제 Task·Gmail·Calendar 데이터를 합치는 일일 브리핑 Workflow."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any, Callable

from app.domain.sync_state import SyncStateRecord
from app.email_action_items import EmailRelevanceProviderError, classify_work_related_emails
from app.providers.google import (
    CalendarEvent,
    GmailAdapter,
    GoogleCalendarAdapter,
    GoogleProviderError,
)
from app.repositories.sync_state import (
    PostgresSyncStateRepository,
    SQLiteSyncStateRepository,
    SyncStateRepository,
)
from app.repositories.tasks import PostgresTaskRepository, SQLiteTaskRepository, TaskRepository
from app.workflows.priority import build_rank_priorities_workflow
from app.workflows.registry import WorkflowRequest, WorkflowResult


GOOGLE_ACCESS_TOKEN_ENV = "WORKMATE_GOOGLE_ACCESS_TOKEN"
GOOGLE_TOKEN_FILE_ENV = "GOOGLE_TOKEN_FILE"
TASK_DB_PATH_ENV = "WORKMATE_TASK_DB_PATH"
DATABASE_URL_ENV = "DATABASE_URL"
_GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]
_DEFAULT_OAUTH_TEST_DIR = Path(__file__).resolve().parents[3] / "google-oauth-test"
"""`app/dev_gmail_sync_api.py`와 같은 상대 경로 값(`google-oauth-test/`)이지만,
이 파일은 `app/workflows/` 아래 한 단계 더 깊이 있어(`app/`이 아니라
`app/workflows/`) `parents[2]`가 아니라 `parents[3]`을 써야 저장소 루트가
나온다."""


class ProviderConfigurationError(RuntimeError):
    """실제 Google Provider 호출에 필요한 런타임 설정이 없는 경우다."""


def _session(user_id: str) -> Any:
    """Google Credentials로 `AuthorizedSession`을 만든다.

    `user_id`가 "제안함" 화면(`app/google_oauth_web.py`)에서 개별로 연결해 둔
    Google 계정이 있으면 그걸 최우선으로 쓴다 — `app/dev_gmail_sync_api.py`·
    `app/dev_calendar_sync_api.py`가 이미 이 순서로 동작한다(2026-08-18). 이
    함수가 그 순서를 빼먹은 채 아래 공유 파일/환경변수 경로만 봤던 탓에, 개별
    연결을 이미 마친 사용자도 "오늘 브리핑" 화면에서는 공유 `token.json`이
    없으면 (환경변수도 없으면) 실패했다 — "제안함"은 되는데 "오늘 브리핑"은
    안 된다는 불일치가 여기서 났다(2026-08-19 실사용 중 발견).

    개별 연결이 없으면 기존 공유 계정 경로로 폴백한다: `GOOGLE_TOKEN_FILE`
    (기본 `google-oauth-test/token.json`)이 있으면 그 Refresh Token으로 항상
    갱신한다 — `app/providers/google_auth.py`가 이 로직을 공유한다(2026-08-17
    통합, 그 모듈의 Docstring에 근본 원인을 적어 뒀다: 저장된 만료
    시각(`credentials.expired`)이 실제 유효성을 보장하지 않아 Calendar 403이
    반복됐다).

    예전엔 `WORKMATE_GOOGLE_ACCESS_TOKEN`(단기 access token만, Refresh 정보
    없음)만 썼는데, 그 방식은 자동 갱신이 불가능해 대략 1시간마다
    "credentials do not contain the necessary fields need to refresh"로
    실패하고 매번 사람이 새 토큰을 발급해 서버를 재기동해야 했다(2026-08-16
    실사용 중 발견). 토큰 파일이 없는 환경(예: `google-oauth-test`를 아직
    설정하지 않은 배포)을 위해 그 단기 토큰 방식도 대체 경로로 남긴다.
    """

    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2.credentials import Credentials
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise ProviderConfigurationError("google-auth transport is unavailable") from exc

    from app.providers.google_auth import (
        GoogleCredentialError,
        build_authorized_session,
        build_authorized_session_for_user,
    )

    if user_id:
        try:
            return build_authorized_session_for_user(user_id, _GOOGLE_SCOPES)
        except GoogleCredentialError:
            pass  # 개별 연결 없음 — 아래 공유 계정으로 폴백

    token_path = Path(os.getenv(GOOGLE_TOKEN_FILE_ENV, str(_DEFAULT_OAUTH_TEST_DIR / "token.json")))
    if token_path.exists():
        try:
            return build_authorized_session(token_path, _GOOGLE_SCOPES)
        except GoogleCredentialError as exc:
            raise ProviderConfigurationError(str(exc)) from exc

    token = os.getenv(GOOGLE_ACCESS_TOKEN_ENV, "")
    if not token:
        raise ProviderConfigurationError(
            f"{GOOGLE_TOKEN_FILE_ENV} or {GOOGLE_ACCESS_TOKEN_ENV} is required for live Google providers"
        )
    return AuthorizedSession(Credentials(token=token))


def _repositories() -> tuple[TaskRepository, SyncStateRepository]:
    """운영 PostgreSQL 또는 로컬 SQLite의 업무·동기화 Repository를 선택한다."""

    dsn = os.getenv(DATABASE_URL_ENV)
    if dsn:
        return PostgresTaskRepository(dsn), PostgresSyncStateRepository(dsn)
    path = os.getenv(TASK_DB_PATH_ENV, ".runtime/tasks.sqlite3")
    return SQLiteTaskRepository(path), SQLiteSyncStateRepository(path)


def _warning(source: str, code: str, message: str, *, retryable: bool) -> dict[str, object]:
    """Provider 부분 실패를 승인 Schema의 Warning 형태로 만든다."""

    return {"source": source, "code": code, "message": message, "retryable": retryable, "last_success_at": None}


def _event_datetime(value: str | None, fallback: datetime) -> str:
    """Calendar date/dateTime을 승인 Schema의 date-time 문자열로 정규화한다."""

    if value and "T" in value:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    return fallback.isoformat()


def build_daily_briefing_workflow(
    *,
    provider_factory: Callable[[], tuple[Any, Any]] | None = None,
    repositories: tuple[TaskRepository, SyncStateRepository] | None = None,
) -> Callable[[WorkflowRequest], Any]:
    """실제 Adapter를 사용하거나 테스트 주입값을 사용하는 브리핑 핸들러를 만든다."""

    task_repository, sync_repository = repositories or _repositories()
    rank_workflow = build_rank_priorities_workflow()

    async def execute(request: WorkflowRequest) -> WorkflowResult:
        """오늘의 일정·메일 신호·Task Top 3를 부분 실패 허용으로 반환한다."""

        payload = dict(request.payload)
        as_of_value = payload.get("as_of")
        as_of = (
            datetime.fromisoformat(str(as_of_value).replace("Z", "+00:00"))
            if as_of_value
            else datetime.now(timezone.utc)
        )
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        timezone_name = str(payload.get("timezone", "Asia/Seoul"))
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(timezone_name)
        local_day = as_of.astimezone(zone).date()
        start = datetime.combine(local_day, time.min, tzinfo=zone)
        end = start + timedelta(days=1)
        warnings: list[dict[str, object]] = []
        calendar_events: list[CalendarEvent] = []
        messages: list[Any] = []
        provider_freshness: dict[str, str | None] = {"calendar": None, "email": None}

        message_summaries: dict[str, str] = {}
        try:
            gmail, calendar = (
                provider_factory()
                if provider_factory
                else (GmailAdapter(_session(request.user_id)), GoogleCalendarAdapter(_session(request.user_id)))
            )
            # `category:primary`로 Gmail이 스스로 분류한 프로모션·소셜·업데이트
            # 탭 메일을 제외한다 — 이게 없으면 "중요 업무 신호"에 사람이 보낸
            # 업무 메일과 구글 마케팅 알림(예: "최근 검색에 표시됨" 뉴스레터)이
            # 구분 없이 섞여 나온다(2026-08-16, 14번 갭 문서 — 실사용 중 발견).
            # `messages.list`는 정렬 조건을 따로 안 받지만 Gmail 검색 결과
            # 기본 순서 자체가 최신 수신순이라, `max_results`로 자르면 곧
            # "최신 N건"이 된다(2026-08-17, 사용자 요청 — 10건도 여전히 많아 5건으로 축소).
            messages = list(gmail.list_messages(query="is:unread category:primary", max_results=5))
            provider_freshness["email"] = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            warnings.append(_warning("email", "GOOGLE_PROVIDER_UNAVAILABLE", str(exc), retryable=getattr(exc, "retryable", True)))
        else:
            # `list_messages`(Gmail `users.messages.list`)는 Snippet·제목을 주지
            # 않는다(GmailAdapter.get_message 참고) — 그대로 쓰면 모든 메일 신호가
            # "읽지 않은 메일 신호"라는 똑같은 자리표시자 문구로만 보인다. 각
            # 메시지의 실제 제목·Snippet을 병렬로 보강한다(순차 호출이면 메일이
            # 많을 때 브리핑 로딩이 메일 수만큼 느려진다). 이 보강은 부가 기능일
            # 뿐이라 실패해도 위의 email 조회 자체가 실패한 것으로 취급하지
            # 않는다 — 별도 `try`로 감싸 실패하면 조용히 자리표시자로 돌아간다
            # (2026-08-16, 14번 갭 문서 — 100건 전부 "읽지 않은 메일 신호"만
            # 보이던 문제 실사용 중 발견).
            try:
                fetched = await asyncio.gather(
                    *(asyncio.to_thread(gmail.get_message, message.message_id) for message in messages),
                    return_exceptions=True,
                )
                for message, detail in zip(messages, fetched):
                    if isinstance(detail, BaseException):
                        continue
                    pieces = [piece for piece in (detail.subject, detail.snippet) if piece]
                    if pieces:
                        message_summaries[message.message_id] = " — ".join(pieces)[:500]
            except Exception:
                pass
            # 광고·뉴스레터·소셜 알림처럼 업무와 무관한 메일까지 "중요 업무
            # 신호"에 그대로 노출됐다(2026-08-17, 사용자 요청) — Gmail의
            # `category:primary`만으로는 사람이 보낸 업무 메일과 Primary로
            # 분류된 광고성 메일이 구분되지 않는다. 제목·Snippet만으로 LLM에
            # 업무 관련 여부를 일괄 판단시켜 걸러낸다. 판단 자체가 실패하면
            # (LLM Provider 오류 등) 이미 가져온 신호를 조용히 숨기지 않고
            # 그대로 둔다 — 이 Workflow가 부분 실패를 허용하는 원칙을 그대로
            # 따른다.
            try:
                relevance = classify_work_related_emails(
                    [
                        (message.message_id, message_summaries.get(message.message_id) or message.snippet or "")
                        for message in messages
                    ]
                )
            except EmailRelevanceProviderError:
                relevance = {}
            messages = [message for message in messages if relevance.get(message.message_id, True)]
        try:
            if "calendar" not in locals():
                _, calendar = (
                    provider_factory()
                    if provider_factory
                    else (GmailAdapter(_session(request.user_id)), GoogleCalendarAdapter(_session(request.user_id)))
                )
            calendar_events, _ = calendar.list_events(time_min=start.astimezone(timezone.utc).isoformat(), time_max=end.astimezone(timezone.utc).isoformat())
            provider_freshness["calendar"] = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            warnings.append(_warning("calendar", "GOOGLE_PROVIDER_UNAVAILABLE", str(exc), retryable=getattr(exc, "retryable", True)))

        rank_result = await rank_workflow(
            WorkflowRequest(
                skill_id="rank_priorities",
                task_id=request.task_id,
                thread_id=request.thread_id,
                message_id=request.message_id,
                user_id=request.user_id,
                payload={**payload, "as_of": as_of.isoformat(), "timezone": timezone_name},
            )
        )
        priorities = json.loads(rank_result.text)["data"]["priorities"]
        # Calendar 일정·Gmail 메일이 이미 Task로 등록됐는지 확인한다 —
        # `source_id` 형식이 승인 시 저장한 값과 같아(Calendar는
        # `{calendar_id}:{event_id}`, Gmail은 `message.id`) 새 매칭 규칙 없이
        # 그대로 대조할 수 있다(2026-08-15, 14번 갭 문서 #32). 이전엔 Calendar
        # 쪽은 항상 빈 배열로 고정돼 있었고 Gmail 쪽은 필드 자체가 없어, 이미
        # 등록한 항목도 브리핑에 매번 "아직 안 한 일"처럼 다시 보였다.
        registered_task_ids: dict[tuple[str, str], str] = {
            (task.source_type, task.source_id): task.task_id
            for task in task_repository.list(request.user_id)
            if task.source_type in ("email", "calendar") and task.source_id
        }

        def _related_task_ids(source_type: str, source_id: str) -> list[str]:
            task_id = registered_task_ids.get((source_type, source_id))
            return [task_id] if task_id else []

        event_items = []
        for event in calendar_events:
            event_start = _event_datetime(event.start, start)
            event_end = _event_datetime(event.end, end)
            event_items.append(
                {
                    "event_id": event.source_id,
                    "title": event.summary or "(제목 없음)",
                    "starts_at": event_start,
                    "ends_at": event_end,
                    "related_task_ids": _related_task_ids("calendar", event.source_id),
                }
            )
        important_signals = [
            {
                "type": "email",
                "source_id": message.message_id,
                "summary": message_summaries.get(message.message_id) or message.snippet or "읽지 않은 메일 신호",
                "related_task_ids": _related_task_ids("email", message.message_id),
            }
            for message in messages
        ]
        source_refs = [item["task_id"] for item in priorities]
        source_refs.extend(event["event_id"] for event in event_items)
        source_refs.extend(signal["source_id"] for signal in important_signals)
        data = {
            "date": local_day.isoformat(),
            "summary": f"오늘 일정 {len(event_items)}건, 메일 신호 {len(important_signals)}건, 우선순위 {len(priorities)}건입니다.",
            "calendar_events": event_items,
            "important_signals": important_signals,
            "priorities": priorities,
            "source_refs": list(dict.fromkeys(source_refs)),
        }
        result_payload = {"type": "daily_briefing", "data": data}
        return WorkflowResult(
            artifact_name="daily_briefing",
            artifact_description="Today briefing from user-owned Tasks and live Google signals.",
            text=json.dumps(result_payload, ensure_ascii=False, sort_keys=True),
            # `data`가 비어 있으면 `internal_chat.py`가 그대로 `artifact.data=null`을
            # 반환하고, 프론트(`useSkillRunner`)는 `response.artifact.data.data`를
            # 읽으므로 요청이 200으로 성공해도 화면엔 항상 `null`만 보인다 —
            # `analyze_meeting`·`search_meetings`·`weekly_report`는 전부 `data=`를
            # 채우는데 이 Workflow만 빠져 있었다(2026-08-16, 14번 갭 문서 — 오늘
            # 브리핑이 로딩·오류 없이 "아직 실행 안 함"에 멈춰 있던 진짜 원인).
            data=result_payload,
            # `text`(JSON 원문)는 기존 소비처(`internal_chat.py`, `test_daily_briefing.py`의
            # `json.loads(result.text)` 검증 7곳)가 그대로 파싱하므로 값을 바꾸지 않는다.
            # `markdown`은 공개 A2A `_artifact_parts()`가 `markdown`이 없을 때만 쓰는 `text/plain`
            # 원문 JSON 대신 사람이 읽을 요약으로 우선 사용하는 필드라 새로 추가만 한다(20번 문서
            # "오케스트레이터 담당자에게 전달할 요청 목록" R1 해결 후 재현: 오케스트레이터 채팅에
            # 원문 JSON이 그대로 노출되던 문제, 2026-08-18).
            markdown=_render_daily_briefing_markdown(data),
            warnings=warnings,
            mock=False,
        )

    return execute


def _render_daily_briefing_markdown(data: dict[str, Any]) -> str:
    """오늘 브리핑 데이터를 사람이 읽기 좋은 Markdown으로 변환한다.

    `weekly_report.py::_render_weekly_report_markdown()`과 같은 원칙 — 구조화 JSON(`data`)을
    원장으로 유지하고, 화면에 바로 보여줄 표현만 결정론적으로(LLM 없이) 생성한다.
    """

    lines = [
        "# 오늘 브리핑",
        "",
        f"날짜: {data.get('date', '')}",
        "",
        str(data.get("summary", "")),
    ]
    calendar_events = data.get("calendar_events") or []
    if calendar_events:
        lines.append("")
        lines.append("## 일정")
        for event in calendar_events:
            title = event.get("title", "(제목 없음)")
            starts_at = event.get("starts_at", "")
            ends_at = event.get("ends_at", "")
            lines.append(f"- {title} ({starts_at} ~ {ends_at})")
    important_signals = data.get("important_signals") or []
    if important_signals:
        lines.append("")
        lines.append("## 메일 신호")
        for signal in important_signals:
            lines.append(f"- {signal.get('summary', '')}")
    priorities = data.get("priorities") or []
    if priorities:
        lines.append("")
        lines.append("## 우선순위")
        for item in priorities:
            title = item.get("title", item.get("task_id", ""))
            reason = item.get("reason", "")
            lines.append(f"- {title}" + (f" — {reason}" if reason else ""))
    return "\n".join(lines)


__all__ = [
    "GOOGLE_ACCESS_TOKEN_ENV",
    "GOOGLE_TOKEN_FILE_ENV",
    "ProviderConfigurationError",
    "build_daily_briefing_workflow",
]
