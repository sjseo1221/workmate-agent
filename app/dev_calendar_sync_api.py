"""실제 Google Calendar 계정으로 제안함(Proposal) 파이프라인을 수동으로 검증하는 개발용 API.

`app/dev_gmail_sync_api.py`와 같은 목적·구조다 — `../google-oauth-test`가 이미 동의를 마친
`token.json`(Gmail·Calendar 두 Scope를 함께 요청해 발급됐다, `google-oauth-test/auth_flow.py`
참고)으로 실제 Calendar 데이터를 읽어 `proposal_hub`에 발행해 `/api/v1/notifications/stream`
→ 제안함 UI까지 실제로 흐르는지 확인하는 용도다.

`GoogleCalendarAdapter`·`CalendarEvent`·`SyncStateRecord`(source_type=google_calendar)·
`publish_calendar_proposal`은 이미 구현돼 있다(`app/providers/google.py`,
`app/domain/sync_state.py`, `app/proposal_api.py`) — 이 모듈은 그 부품들을 실제로 호출하는
HTTP 진입점만 새로 연결한다.

기본값은 비활성화이며 `WORKMATE_DEV_CALENDAR_SYNC_ENABLED=true`일 때만 열린다.
"""

from __future__ import annotations

import calendar
import hmac
import logging
import os
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Response, status
from google.auth.transport.requests import AuthorizedSession

from app.domain.sync_state import SyncStateRecord
from app.internal_chat import _authenticated_user_or_assignee
from app.proposal_api import ignored_proposal_repository, publish_calendar_proposal
from app.providers.google import CalendarEvent, GoogleCalendarAdapter, GoogleProviderError, SyncCursorExpiredError
from app.providers.google_auth import (
    GoogleCredentialError,
    build_authorized_session,
    build_authorized_session_for_user,
)
from app.repositories.sync_state import (
    PostgresSyncStateRepository,
    SQLiteSyncStateRepository,
    SyncStateRepository,
)
from app.task_api import DATABASE_URL_ENV, TASK_DB_PATH_ENV, task_repository

logger = logging.getLogger("workmate-agent.dev-calendar-sync")

DEV_CALENDAR_SYNC_ENABLED_ENV = "WORKMATE_DEV_CALENDAR_SYNC_ENABLED"
CLIENT_SECRET_FILE_ENV = "GOOGLE_CLIENT_SECRET_FILE"
TOKEN_FILE_ENV = "GOOGLE_TOKEN_FILE"
CALENDAR_ID_ENV = "GOOGLE_CALENDAR_ID"
CALENDAR_WATCH_ADDRESS_ENV = "GOOGLE_CALENDAR_WATCH_ADDRESS"
# 다른 Workflow(daily_briefing·weekly_report 등)와 같은 기본 timezone이다 —
# 이 모듈엔 사용자별 timezone을 받는 입력이 없어(Bearer Token 인증만 받는
# Body 없는 POST) 고정값을 쓴다(2026-08-17, "오늘 이후" 제안 필터 요청).
_DEFAULT_TIMEZONE = "Asia/Seoul"
_DEFAULT_OAUTH_TEST_DIR = Path(__file__).resolve().parents[2] / "google-oauth-test"
# `google-oauth-test/auth_flow.py`가 두 Scope를 함께 요청해 token.json을 발급하므로
# 여기서도 두 Scope를 함께 선언한다 — Gmail 전용 Scope만 넘기면 라이브러리가 저장된
# Credential의 Scope와 다르다고 판단해 재동의를 요구할 수 있다.
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]

router = APIRouter(prefix="/api/v1/dev", tags=["dev-calendar-sync"])
webhook_router = APIRouter(tags=["calendar-webhook"])

# Channel ID → 소유 사용자 역인덱스. `SyncStateRepository`는 (user_id, source_type)로만
# 조회할 수 있어 Webhook이 들고 오는 Channel ID만으로는 사용자를 못 찾는다. 등록 시
# 채워두는 조회용 캐시일 뿐 원장이 아니다 — 실제 sync_cursor·Watch 정보는 항상
# `SyncStateRepository`에서 다시 읽는다. 백엔드가 재기동하면 이 인덱스는 비워지므로
# `/calendar-watch/register`를 다시 호출해야 Webhook이 사용자를 다시 찾을 수 있다
# (Gmail Push의 `_watch_state`와 같은 한계 — 두 Provider 모두 M1.1에서 DB 기반 역인덱스로
# 고쳐야 하는 이미 알려진 이슈).
_channel_index: dict[str, str] = {}


def _enabled() -> bool:
    return os.getenv(DEV_CALENDAR_SYNC_ENABLED_ENV, "").lower() == "true"


def _credential_paths() -> tuple[Path, Path]:
    secret = Path(os.getenv(CLIENT_SECRET_FILE_ENV, str(_DEFAULT_OAUTH_TEST_DIR / "client_secret.json")))
    token = Path(os.getenv(TOKEN_FILE_ENV, str(_DEFAULT_OAUTH_TEST_DIR / "token.json")))
    return secret, token


def _load_authorized_session(user_id: str) -> AuthorizedSession:
    """`user_id`가 개별로 연결한 Google 계정이 있으면 그걸 쓰고, 없으면 기존 공유
    `token.json`(고정 데모 계정)으로 폴백한다.

    `app/dev_gmail_sync_api.py`의 같은 이름 함수와 동작이 같다(2026-08-18 확장 —
    거기 Docstring 참고). 서버 프로세스에는 대화형 브라우저가 없으므로, 공유 계정
    Token도 없거나 Refresh에 실패하면 안내하고 503으로 거부한다. 실제 Credential
    로드·갱신은 `app/providers/google_auth.py`가 공통으로 담당한다 — Refresh
    Token이 있으면 `credentials.expired` 값과 무관하게 매번 강제로 갱신한다(그
    모듈의 Docstring 참고, 저장된 만료 시각이 실제 유효성을 보장하지 않아서다.
    2026-08-17, "Google authorization required" 반복 발생 근본 수정).
    """

    try:
        return build_authorized_session_for_user(user_id, _SCOPES)
    except GoogleCredentialError:
        pass  # 개별 연결 없음 — 아래 공유 계정으로 폴백

    secret_path, token_path = _credential_paths()
    if not token_path.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                "연결된 Google 계정이 없습니다. 화면에서 'Google 계정 연결'을 누르세요. "
                f"(개발 환경 공유 계정을 쓰려면 {token_path}가 있는지 확인 — "
                "google-oauth-test에서 `python auth_flow.py` 먼저 실행)"
            ),
        )
    try:
        return build_authorized_session(token_path, _SCOPES)
    except GoogleCredentialError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"{exc} 화면에서 'Google 계정 연결'을 다시 누르거나, google-oauth-test에서 재동의하세요 (Client Secret: {secret_path}).",
        ) from exc


def sync_state_repository() -> SyncStateRepository:
    """`task_repository()`와 같은 DB 선택 기준(운영 PostgreSQL/로컬 SQLite)을 공유한다."""

    dsn = os.getenv(DATABASE_URL_ENV)
    if dsn:
        return PostgresSyncStateRepository(dsn)
    return SQLiteSyncStateRepository(os.getenv(TASK_DB_PATH_ENV, ".runtime/tasks.sqlite3"))


def _calendar_id() -> str:
    return os.getenv(CALENDAR_ID_ENV, "primary")


def _add_one_month(value: date) -> date:
    """`value`의 정확히 한 달 뒤 날짜를 반환한다 — 같은 일자, 그 달에 없으면 마지막 날로 보정한다
    (예: 1/31 → 2/28)."""

    year, month = (value.year + 1, 1) if value.month == 12 else (value.year, value.month + 1)
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(value.day, last_day))


def _today_window(now: datetime, *, timezone_name: str = _DEFAULT_TIMEZONE) -> tuple[datetime, datetime]:
    """`timezone_name`(기본 Asia/Seoul) 기준 "오늘부터 한 달"의 반열린 UTC 범위를 반환한다.

    2026-08-17 사용자 요청("제안함 Calendar 필터: 오늘 이후 → 오늘부터 한달")으로
    상한을 추가했다 — 끝없이 먼 미래 일정까지 제안으로 쌓이지 않게 한다.
    """

    zone = ZoneInfo(timezone_name)
    local_today = now.astimezone(zone).date()
    start = datetime.combine(local_today, time.min, tzinfo=zone)
    end = datetime.combine(_add_one_month(local_today), time.min, tzinfo=zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _parse_event_start(event: CalendarEvent) -> datetime | None:
    """Calendar Event 시작 시각을 timezone-aware datetime으로 정규화한다.

    `start`는 종일 일정이면 `YYYY-MM-DD`, 시간 일정이면 RFC3339다. 두 형식을
    모두 받아 파싱하며, 파싱할 수 없는 값은 `None`을 반환한다.
    """

    if not event.start:
        return None
    raw = event.start.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(f"{raw}T00:00:00+00:00")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_within_today_window(event: CalendarEvent, *, today_start: datetime, today_end: datetime) -> bool:
    """04-scenarios.md §8의 "신규·확정·미래 일정만" 규칙을 적용한다.

    범위를 "지금 이 순간 이후"가 아니라 "오늘 로컬 자정부터 한 달"로 둔다
    (2026-08-17, 사용자 요청 "제안함에 calendar는 오늘부터 한달") — 이미
    시작한 오늘 일정도 포함하고, 어제까지의 일정과 한 달 넘게 남은 일정은
    제외한다. 예전 `now` 기준 비교는 부작용도 있었다: 종일 일정은 항상 UTC
    자정으로 파싱되는데(`_parse_event_start`), 로컬(KST) 오후 시간대에는 그
    UTC 자정이 이미 지나 있어 "오늘의 종일 일정"이 오후부터 조용히 걸러지는
    시간대 버그가 있었다.
    """

    if event.status != "confirmed":
        return False
    parsed = _parse_event_start(event)
    if parsed is None:
        return False
    return today_start <= parsed < today_end


def _already_approved(user_id: str, source_id: str) -> bool:
    """이 Calendar Event에서 이미 승인된 Task가 있는지 확인한다."""

    return any(
        task.source_type == "calendar" and task.source_id == source_id
        for task in task_repository().list(user_id)
    )


def _already_ignored(user_id: str, source_id: str) -> bool:
    """이 Calendar Event를 사용자가 이미 "무시"했는지 확인한다(2026-08-16, 17번 갭 문서 #2).

    이전엔 이 기록 자체가 없어 수동 동기화·Webhook 증분 동기화 둘 다
    이미 무시한 일정을 다시 제안으로 발행했다 — 특히 Webhook은 일정이
    나중에 다시 갱신되면(다른 사람이 편집 등) 무시했던 일정도 "변경됨"
    목록에 다시 잡혀 더 자주 재노출됐다.
    """

    return source_id in ignored_proposal_repository().ignored_source_ids(user_id, "calendar")


@router.post("/calendar-sync")
async def trigger_calendar_sync(user_id: str = Depends(_authenticated_user_or_assignee)) -> dict[str, Any]:
    """오늘부터 한 달 안의 확정 일정 중 아직 제안·승인되지 않은 것만 제안함 SSE로 발행한다."""

    if not _enabled():
        raise HTTPException(status_code=404, detail="dev calendar sync is disabled")
    session = _load_authorized_session(user_id)
    adapter = GoogleCalendarAdapter(session, calendar_id=_calendar_id())
    now = datetime.now(timezone.utc)
    today_start, today_end = _today_window(now)
    try:
        events, _next_token = adapter.list_events(time_min=today_start.isoformat(), time_max=today_end.isoformat())
    except GoogleProviderError as exc:
        raise HTTPException(status_code=exc.status_code if exc.status_code >= 400 else 502, detail=str(exc)) from exc

    published: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for event in events:
        if not _is_within_today_window(event, today_start=today_start, today_end=today_end):
            skipped.append({"source_id": event.source_id, "reason": "not_confirmed_or_out_of_range"})
            continue
        if _already_approved(user_id, event.source_id):
            skipped.append({"source_id": event.source_id, "reason": "already_approved"})
            continue
        if _already_ignored(user_id, event.source_id):
            skipped.append({"source_id": event.source_id, "reason": "already_ignored"})
            continue
        await publish_calendar_proposal(user_id, event, due_at=_parse_event_start(event))
        published.append({"source_id": event.source_id, "title": event.summary})

    return {
        "fetched": len(events),
        "published": published,
        "skipped": skipped,
        "note": "신규·확정·오늘부터 한 달 안의 일정만 할 일 후보로 제안합니다. 그 밖의 일정(어제까지·한 달 초과)·미확정·반복 일정과 이미 승인된 일정은 제외됩니다.",
    }


@router.post("/calendar-watch/register")
async def register_calendar_watch(user_id: str = Depends(_authenticated_user_or_assignee)) -> dict[str, Any]:
    """Calendar Push Webhook을 등록하고 `SyncStateRepository`에 영속 저장한다.

    Gmail Watch(`app/dev_gmail_sync_api.py`)는 프로세스 메모리 딕셔너리에만
    등록 상태를 두어 백엔드 재기동 시 사라진다. Calendar는 `SyncStateRecord`에
    Watch 필드(`channel_id`/`channel_token`/`resource_id`/`watch_expiration`)가
    이미 준비돼 있으므로 처음부터 영속 저장소를 사용한다.
    """

    if not _enabled():
        raise HTTPException(status_code=404, detail="dev calendar sync is disabled")
    address = os.getenv(CALENDAR_WATCH_ADDRESS_ENV, "").strip()
    if not address:
        raise HTTPException(status_code=503, detail=f"{CALENDAR_WATCH_ADDRESS_ENV}가 설정되지 않았습니다.")
    session = _load_authorized_session(user_id)
    adapter = GoogleCalendarAdapter(session, calendar_id=_calendar_id())
    channel_id = str(uuid4())
    channel_token = uuid4().hex
    try:
        watch = adapter.watch(channel_id=channel_id, address=address, token=channel_token)
    except GoogleProviderError as exc:
        raise HTTPException(status_code=exc.status_code if exc.status_code >= 400 else 502, detail=str(exc)) from exc
    expiration_ms = watch.get("expiration")
    watch_expiration = (
        datetime.fromtimestamp(int(expiration_ms) / 1000, tz=timezone.utc) if expiration_ms else None
    )
    repository = sync_state_repository()
    existing = repository.get(user_id, "google_calendar")
    record = repository.upsert(
        SyncStateRecord(
            source_sync_state_id=existing.source_sync_state_id if existing else str(uuid4()),
            sync_user_id=user_id,
            source_type="google_calendar",
            sync_cursor=existing.sync_cursor if existing else None,
            channel_id=channel_id,
            channel_token=channel_token,
            resource_id=str(watch.get("resourceId", "")),
            watch_expiration=watch_expiration,
            last_synced_at=existing.last_synced_at if existing else None,
            status="idle",
        )
    )
    _channel_index[channel_id] = user_id
    logger.info(
        "calendar_watch_registered user_id=%s channel_id=%s resource_id=%s expiration=%s",
        user_id, channel_id, record.resource_id, record.watch_expiration,
    )
    return {
        "channel_id": record.channel_id,
        "resource_id": record.resource_id,
        "expiration": record.watch_expiration.isoformat() if record.watch_expiration else None,
    }


@router.get("/calendar-watch/status")
def calendar_watch_status(user_id: str = Depends(_authenticated_user_or_assignee)) -> dict[str, Any]:
    """현재 영속 저장된 Watch 등록 상태를 그대로 보여준다."""

    if not _enabled():
        raise HTTPException(status_code=404, detail="dev calendar sync is disabled")
    state = sync_state_repository().get(user_id, "google_calendar")
    if state is None:
        return {"registered": False}
    return {
        "registered": True,
        "channel_id": state.channel_id,
        "resource_id": state.resource_id,
        "expiration": state.watch_expiration.isoformat() if state.watch_expiration else None,
        "last_synced_at": state.last_synced_at.isoformat() if state.last_synced_at else None,
    }


async def _run_calendar_incremental_sync(user_id: str) -> None:
    """Webhook 검증 후 백그라운드로 실행하는 실제 증분 동기화.

    Calendar Webhook 본문에는 일정 내용이 전혀 없으므로(변경됐다는 신호뿐)
    항상 `events.list(syncToken=...)`로 다시 조회해야 한다.
    """

    repository = sync_state_repository()
    state = repository.get(user_id, "google_calendar")
    if state is None:
        logger.warning("calendar_webhook_sync_skipped user_id=%s reason=no_watch_registered", user_id)
        return
    try:
        session = _load_authorized_session(user_id)
    except HTTPException as exc:
        logger.warning("calendar_webhook_sync_auth_failed user_id=%s detail=%s", user_id, exc.detail)
        return
    adapter = GoogleCalendarAdapter(session, calendar_id=_calendar_id())
    now = datetime.now(timezone.utc)
    today_start, today_end = _today_window(now)
    try:
        events, next_token = adapter.list_events(sync_token=state.sync_cursor)
    except SyncCursorExpiredError:
        logger.warning("calendar_webhook_cursor_expired user_id=%s — 초기 동기화로 재설정", user_id)
        events, next_token = adapter.list_events(time_min=today_start.isoformat(), time_max=today_end.isoformat())
    except GoogleProviderError as exc:
        logger.warning("calendar_webhook_sync_failed user_id=%s error=%s", user_id, exc)
        return

    published = 0
    for event in events:
        if not _is_within_today_window(event, today_start=today_start, today_end=today_end) or _already_approved(user_id, event.source_id):
            continue
        if _already_ignored(user_id, event.source_id):
            continue
        await publish_calendar_proposal(user_id, event, due_at=_parse_event_start(event))
        published += 1

    if next_token:
        repository.upsert(
            SyncStateRecord(
                source_sync_state_id=state.source_sync_state_id,
                sync_user_id=user_id,
                source_type="google_calendar",
                sync_cursor=next_token,
                channel_id=state.channel_id,
                channel_token=state.channel_token,
                resource_id=state.resource_id,
                watch_expiration=state.watch_expiration,
                last_synced_at=now,
                status="succeeded",
            )
        )
    logger.info("calendar_webhook_sync_done user_id=%s new_events=%s published=%s", user_id, len(events), published)


@webhook_router.post("/webhooks/google-calendar", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def receive_calendar_webhook(
    background_tasks: BackgroundTasks,
    response: Response,
    x_goog_channel_id: str | None = Header(default=None),
    x_goog_channel_token: str | None = Header(default=None),
    x_goog_resource_id: str | None = Header(default=None),
) -> None:
    """Google Calendar Watch Webhook 대상.

    Google은 빠른 응답을 기대하므로 헤더 검증까지만 마치고 바로 204를
    반환한 뒤, 실제 `events.list` 재조회와 제안 발행은 `BackgroundTasks`로
    미룬다. 검증 실패는 재시도 폭주를 피하기 위해 여기서도 조용히 204로
    끝낸다 — 유효하지 않은 Channel의 반복 호출을 막을 필요가 생기면
    이때 401로 바꾼다.
    """

    if not _enabled():
        response.status_code = status.HTTP_404_NOT_FOUND
        return None
    if not x_goog_channel_id or not x_goog_channel_token or not x_goog_resource_id:
        return None

    user_id = _channel_index.get(x_goog_channel_id)
    if user_id is None:
        # 등록되지 않았거나(백엔드 재기동으로 인덱스가 비워졌거나) 다른 Channel의
        # 알림이다. Gmail Push와 같은 이유로 4xx 대신 조용히 끝낸다 — 잘못
        # 거절하면 Google이 같은 알림을 계속 재시도해 쌓인다.
        logger.warning("calendar_webhook_ignored reason=no_matching_channel channel_id=%s", x_goog_channel_id)
        return None

    state = sync_state_repository().get(user_id, "google_calendar")
    if state is None or not (
        hmac.compare_digest(state.channel_id or "", x_goog_channel_id)
        and hmac.compare_digest(state.channel_token or "", x_goog_channel_token)
        and hmac.compare_digest(state.resource_id or "", x_goog_resource_id)
    ):
        logger.warning("calendar_webhook_rejected reason=channel_mismatch channel_id=%s", x_goog_channel_id)
        return None

    background_tasks.add_task(_run_calendar_incremental_sync, user_id)
    return None


__all__ = [
    "router",
    "webhook_router",
    "DEV_CALENDAR_SYNC_ENABLED_ENV",
    "CLIENT_SECRET_FILE_ENV",
    "TOKEN_FILE_ENV",
    "CALENDAR_ID_ENV",
    "CALENDAR_WATCH_ADDRESS_ENV",
    "sync_state_repository",
]
