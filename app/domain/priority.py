"""Task 우선순위 점수와 결정적 정렬을 담당하는 순수 Domain 함수."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.task import TaskRecord


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """승인된 우선순위 점수 구성."""

    deadline: int
    importance: int
    blocked_or_overdue: int
    meeting_commitment: int
    calendar_relevance: int

    @property
    def total(self) -> int:
        """구성 요소를 합산한 총점을 반환한다."""

        return sum(
            (
                self.deadline,
                self.importance,
                self.blocked_or_overdue,
                self.meeting_commitment,
                self.calendar_relevance,
            )
        )


@dataclass(frozen=True, slots=True)
class RankedTask:
    """우선순위 계산 결과와 표시용 근거."""

    task: TaskRecord
    score: int
    breakdown: ScoreBreakdown
    reasons: tuple[str, ...]
    rank: int


def _zone(timezone_name: str) -> ZoneInfo:
    """IANA timezone을 검증해 반환한다."""

    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unsupported timezone: {timezone_name}") from exc


def _deadline_score(task: TaskRecord, as_of: datetime, zone: ZoneInfo) -> int:
    """사용자 지역 날짜 기준으로 마감임박도 점수를 계산한다."""

    if task.due_at is None:
        return 0
    days = (task.due_at.astimezone(zone).date() - as_of.astimezone(zone).date()).days
    if days < 0:
        return 40
    if days == 0:
        return 35
    if days == 1:
        return 30
    if days <= 3:
        return 20
    if days <= 7:
        return 10
    return 0


def _importance_score(priority_hint: int | None) -> int:
    """Task의 0~10 힌트를 승인된 중요도 구간으로 변환한다."""

    if priority_hint is None or priority_hint <= 2:
        return 0
    if priority_hint <= 4:
        return 8
    if priority_hint <= 7:
        return 15
    return 20


def _blocked_or_overdue_score(task: TaskRecord, as_of: datetime) -> int:
    """차단 또는 기한 초과 점수를 계산하며 최대 15점으로 제한한다."""

    blocked = task.status == "blocked"
    overdue = task.due_at is not None and task.due_at < as_of
    if blocked:
        return 15
    return 10 if overdue else 0


def _calendar_relevance_score(task: TaskRecord, as_of: datetime) -> int:
    """03 §6 "일정 연관"(24시간 내 10, 3일 내 5) 점수를 계산한다.

    `meeting_commitment`이 이미 "회의 Action Item에서 승인된 Task"를 담당하므로
    (`source_type="action_item"`), 여기서는 겹치지 않게 **Calendar 일정에서
    승인된 Task**(`source_type="calendar"`)만 대상으로 한다. `due_at`은 Calendar
    제안을 승인할 때 일정 시작 시각으로 채워진다(2026-08-17,
    `app/dev_calendar_sync_api.py`의 `_parse_event_start`) — 그 전엔 Calendar
    제안의 `due_at`이 항상 `null`이라 이 점수가 계산될 방법 자체가 없었다.

    지난 일정(이미 시작한)도 아직 처리 안 됐다면 여전히 "관련"으로 본다 —
    방향 대신 `as_of`와의 거리(절댓값)만 본다.
    """

    if task.source_type != "calendar" or task.due_at is None:
        return 0
    delta = abs(task.due_at - as_of)
    if delta <= timedelta(hours=24):
        return 10
    if delta <= timedelta(days=3):
        return 5
    return 0


def score_task(
    task: TaskRecord,
    *,
    as_of: datetime,
    timezone_name: str,
    meeting_commitment: int = 0,
    calendar_relevance: int = 0,
) -> tuple[int, ScoreBreakdown, tuple[str, ...]]:
    """Task 하나의 점수·구성·결정적 표시 근거를 계산한다.

    Args:
        task: 점수 계산 대상 Task.
        as_of: 기준 시각. timezone 정보가 없는 값은 거부한다.
        timezone_name: 조회 사용자의 IANA timezone.
        meeting_commitment: 승인된 Action Item이면 10, 아니면 0.
        calendar_relevance: 관련 일정이 24시간 이내면 10, 3일 이내면 5, 아니면 0.
    Raises:
        ValueError: timezone, 기준 시각, 외부 점수 구성 값이 유효하지 않은 경우.
    """

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if meeting_commitment not in {0, 10}:
        raise ValueError("meeting_commitment must be 0 or 10")
    if calendar_relevance not in {0, 5, 10}:
        raise ValueError("calendar_relevance must be 0, 5, or 10")
    zone = _zone(timezone_name)
    breakdown = ScoreBreakdown(
        deadline=_deadline_score(task, as_of, zone),
        importance=_importance_score(task.priority_hint),
        blocked_or_overdue=_blocked_or_overdue_score(task, as_of.astimezone(timezone.utc)),
        meeting_commitment=meeting_commitment,
        calendar_relevance=calendar_relevance,
    )
    reasons: list[str] = []
    if breakdown.deadline == 40:
        reasons.append("기한 초과")
    elif breakdown.deadline == 35:
        reasons.append("오늘 마감")
    elif breakdown.deadline:
        reasons.append("마감 임박")
    if breakdown.importance:
        reasons.append("중요도 신호")
    if breakdown.blocked_or_overdue == 15:
        reasons.append("차단됨")
    elif breakdown.blocked_or_overdue == 10:
        reasons.append("지연됨")
    if meeting_commitment:
        reasons.append("승인된 회의 Action Item")
    if calendar_relevance:
        reasons.append("관련 일정 임박")
    return breakdown.total, breakdown, tuple(reasons or ("추가 우선순위 신호 없음",))


def rank_tasks(
    tasks: list[TaskRecord],
    *,
    as_of: datetime,
    timezone_name: str,
    top_n: int = 3,
) -> list[RankedTask]:
    """완료·취소·Soft Delete Task를 제외하고 결정적으로 Top N을 계산한다."""

    if top_n < 1:
        raise ValueError("top_n must be positive")
    candidates: list[tuple[TaskRecord, int, ScoreBreakdown, tuple[str, ...]]] = []
    for task in tasks:
        if task.deleted_at is not None or task.status in {"done", "cancelled"}:
            continue
        total, breakdown, reasons = score_task(
            task,
            as_of=as_of,
            timezone_name=timezone_name,
            meeting_commitment=10 if task.source_type == "action_item" else 0,
            calendar_relevance=_calendar_relevance_score(task, as_of),
        )
        candidates.append((task, total, breakdown, reasons))

    def sort_key(item: tuple[TaskRecord, int, ScoreBreakdown, tuple[str, ...]]) -> tuple[object, ...]:
        task, total, _, _ = item
        due_key = task.due_at.astimezone(timezone.utc) if task.due_at else datetime.max.replace(tzinfo=timezone.utc)
        updated_key = -(task.updated_at or datetime.min.replace(tzinfo=timezone.utc)).timestamp()
        return (-total, due_key, updated_key, task.task_id)

    ranked: list[RankedTask] = []
    for rank, (task, total, breakdown, reasons) in enumerate(sorted(candidates, key=sort_key)[:top_n], start=1):
        ranked.append(RankedTask(task, total, breakdown, reasons, rank))
    return ranked
