"""주간 보고서의 기간과 사용자 조회 범위를 정규화하는 Domain 모듈."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.task import TaskRecord

WeeklyBucket = Literal["completed", "in_progress", "delayed", "issue", "excluded"]


@dataclass(frozen=True, slots=True)
class WeeklyPeriod:
    """사용자 timezone 기준 주간 조회의 반열린 시간 범위."""

    week_of: date
    timezone_name: str
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True, slots=True)
class WeeklyScope:
    """주간 분류 조회에 적용할 사용자와 기간의 불변 범위."""

    user_id: str
    period: WeeklyPeriod
    as_of: datetime


def classify_task_status(task: TaskRecord, *, as_of: datetime) -> WeeklyBucket:
    """Task를 주간 보고용 상태 한 가지로 분류한다.

    Soft Delete와 `cancelled`는 `excluded`로 분류해 보고서 목록에서 제외한다.
    `blocked`는 `issue`(미해결 이슈)로, 완료·취소가 아닌 기한 초과 Task는
    `delayed`로 분류하며, `done`은 `completed`, 나머지 활성 상태는
    `in_progress`가 된다.

    `issue`와 `delayed`를 나누는 기준(2026-08-16, 17번 갭 문서 #16): `blocked`는
    "누가 봐도 막혀서 외부 조치가 필요한" 상태이고, `delayed`는 "그냥 기한을
    넘긴" 상태다 — 06번 문서의 M2.1 범위("완료·진행·지연·이슈 집계")와 11번
    문서의 LLM 평가 이력("blocked Task가 `unresolved_issues`에서 누락됐다",
    2026-08-10 수정)이 이미 이 구분을 전제하고 있었다. `blocked`이면서 기한도
    지났어도 `issue`를 우선한다(막힌 원인 해소가 먼저라 기한 초과 표시는
    부차적).

    Args:
        task: 분류할 업무 Task.
        as_of: 사용자 timezone으로 정규화된 기준 시각.
    Raises:
        ValueError: 기준 시각 또는 마감 시각이 timezone 없는 값인 경우.
    """

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if task.deleted_at is not None or task.status == "cancelled":
        return "excluded"
    if task.status == "done":
        return "completed"
    if task.status == "blocked":
        return "issue"
    if task.due_at is not None:
        if task.due_at.tzinfo is None or task.due_at.utcoffset() is None:
            raise ValueError("task.due_at must be timezone-aware")
        if task.due_at < as_of:
            return "delayed"
    return "in_progress"


def _zone(timezone_name: str) -> ZoneInfo:
    """IANA timezone 이름을 검증해 반환한다."""

    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unsupported timezone: {timezone_name}") from exc


def _week_date(value: date | str) -> date:
    """ISO 날짜 또는 date 값을 주간 기준일로 정규화한다."""

    if isinstance(value, datetime):
        raise ValueError("week_of must be a date, not datetime")
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        pass
    # 순수 `YYYY-MM-DD`가 아니라 시각·timezone까지 붙은 전체 ISO 8601
    # 문자열("2026-08-10T00:00:00+09:00")이 오면 위에서 바로 실패했다 —
    # 자연어 Router가 채우는 값이라 가끔 이 형식으로 나온다(2026-08-17,
    # 실사용 중 발견). 날짜만 뽑아 쓴다 — 이것도 아니면 원래 오류를 낸다.
    try:
        return datetime.fromisoformat(str(value)).date()
    except (TypeError, ValueError) as exc:
        raise ValueError("week_of must be an ISO date") from exc


def normalize_week_scope(
    user_id: str,
    week_of: date | str,
    timezone_name: str,
    *,
    as_of: datetime | str | None = None,
) -> WeeklyScope:
    """사용자·timezone·주간 기준일을 결정적인 조회 범위로 만든다.

    주간 범위는 현지 시간의 `week_of` 00:00 이상, 7일 뒤 현지 자정 미만이다.
    DST 전환 주에도 현지 달력 경계를 유지하며, 모든 후속 Repository 조회는
    반환된 `user_id`를 반드시 조건으로 사용해야 한다.

    Args:
        user_id: 인증된 OIDC subject. 빈 값은 허용하지 않는다.
        week_of: 주간 범위의 현지 기준일(ISO `YYYY-MM-DD` 또는 `date`).
        timezone_name: IANA timezone 이름.
        as_of: 기준 시각. 생략하면 현재 시각을 사용하며, 문자열은 ISO 8601이어야 한다.
    Raises:
        ValueError: 사용자·날짜·timezone·기준 시각이 유효하지 않은 경우.
    """

    normalized_user_id = user_id.strip()
    if not normalized_user_id:
        raise ValueError("user_id must not be empty")
    zone = _zone(timezone_name)
    normalized_week = _week_date(week_of)
    start_at = datetime.combine(normalized_week, time.min, tzinfo=zone)
    end_at = datetime.combine(normalized_week + timedelta(days=7), time.min, tzinfo=zone)

    if as_of is None:
        normalized_as_of = datetime.now(zone)
    elif isinstance(as_of, str):
        try:
            normalized_as_of = datetime.fromisoformat(as_of)
        except ValueError as exc:
            raise ValueError("as_of must be ISO 8601") from exc
    else:
        normalized_as_of = as_of
    if normalized_as_of.tzinfo is None or normalized_as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")

    period = WeeklyPeriod(
        week_of=normalized_week,
        timezone_name=timezone_name,
        start_at=start_at,
        end_at=end_at,
    )
    return WeeklyScope(
        user_id=normalized_user_id,
        period=period,
        as_of=normalized_as_of.astimezone(zone),
    )
