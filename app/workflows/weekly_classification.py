"""주간 보고서용 승인 Task와 원본 ID를 연결하는 Workflow 경계."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, NamedTuple

from app.domain.task import TaskRecord
from app.domain.weekly import WeeklyBucket, WeeklyScope, classify_task_status


@dataclass(frozen=True, slots=True)
class WeeklyTaskItem:
    """주간 보고서 분류 결과와 추적 가능한 원본 참조를 함께 보관한다."""

    task: TaskRecord
    bucket: WeeklyBucket
    source_refs: tuple[str, ...]


class ExplicitPlan(NamedTuple):
    """원본 문장에서 추출한 계획 후보와 그 근거 ID."""

    title: str
    source_refs: tuple[str, ...]


_BUCKET_ORDER: dict[WeeklyBucket, int] = {
    "completed": 0,
    "in_progress": 1,
    "delayed": 2,
    "issue": 3,
    "excluded": 4,
}


def _source_refs(task: TaskRecord) -> tuple[str, ...]:
    """Task 원장 ID와 승인된 외부 원본 ID를 중복 없이 반환한다."""

    refs = [task.task_id]
    if task.source_id:
        refs.append(task.source_id)
    return tuple(dict.fromkeys(refs))


def link_approved_tasks(
    tasks: Iterable[TaskRecord],
    *,
    scope: WeeklyScope,
) -> tuple[WeeklyTaskItem, ...]:
    """사용자 소유의 저장 Task를 주간 분류와 원본 참조로 연결한다.

    Gmail·Calendar 승인 제안은 Task 원장에 저장된 시점이 승인 경계다. 따라서
    이 함수는 Provider의 미승인 신호를 직접 읽지 않고, 저장된 Task만 포함한다.
    다른 사용자의 Task, Soft Delete, `cancelled` 상태는 결과에서 제외해
    보고서가 사용자 범위를 넘지 않도록 한다.

    Args:
        tasks: Task Repository에서 조회한 후보 목록.
        scope: 사용자·기준 시각이 정규화된 주간 조회 범위.
    Returns:
        결정적인 순서로 정렬된 주간 Task 항목.
    """

    linked: list[WeeklyTaskItem] = []
    for task in tasks:
        if task.assignee_user_id != scope.user_id:
            continue
        bucket = classify_task_status(task, as_of=scope.as_of)
        if bucket == "excluded":
            continue
        linked.append(
            WeeklyTaskItem(
                task=task,
                bucket=bucket,
                source_refs=_source_refs(task),
            )
        )
    linked.sort(key=lambda item: (_BUCKET_ORDER[item.bucket], item.task.task_id))
    return tuple(linked)


def link_action_item_tasks(
    tasks: Iterable[TaskRecord],
    *,
    scope: WeeklyScope,
    known_action_item_ids: Iterable[str],
) -> tuple[WeeklyTaskItem, ...]:
    """검증된 Action Item 원본에 연결된 사용자 Task만 반환한다.

    실제 회의 원본 저장소가 준비되기 전에도, 호출자가 현재 조회한
    `known_action_item_ids` 집합을 명시해 미존재 원본을 차단할 수 있다.
    이 경계는 임의의 `source_id`를 보고서 근거로 신뢰하지 않는다.
    """

    known = {value for value in known_action_item_ids if value}
    linked: list[WeeklyTaskItem] = []
    for task in tasks:
        if task.assignee_user_id != scope.user_id or task.source_type != "action_item":
            continue
        if task.source_id not in known:
            continue
        bucket = classify_task_status(task, as_of=scope.as_of)
        if bucket == "excluded":
            continue
        linked.append(
            WeeklyTaskItem(
                task=task,
                bucket=bucket,
                source_refs=_source_refs(task),
            )
        )
    linked.sort(key=lambda item: (_BUCKET_ORDER[item.bucket], item.task.task_id))
    return tuple(linked)


def build_weekly_report_data(
    items: Iterable[WeeklyTaskItem],
    *,
    scope: WeeklyScope,
) -> dict[str, object]:
    """분류된 Task를 `weekly_report` Artifact 입력 형태로 만든다.

    LLM이 상태나 출처를 새로 만들지 않도록 모든 WorkItem과 최상위
    `source_refs`를 저장된 Task 항목에서만 구성한다. 계획은 아직 별도
    원장이 없으므로 빈 배열로 명시하며, 후속 Workflow가 채운다. `issue`
    Bucket(`blocked` Task)은 `unresolved_issues` 키로 옮겨 담는다 —
    Bucket 이름과 응답 필드 이름이 다르다(2026-08-16, 17번 갭 문서 #16).
    """

    grouped: dict[str, list[dict[str, object]]] = {
        "completed": [],
        "in_progress": [],
        "delayed": [],
        "issue": [],
    }
    source_refs: list[str] = []
    for item in items:
        if item.bucket not in grouped:
            continue
        task = item.task
        grouped[item.bucket].append(
            {
                "task_id": task.task_id,
                "title": task.title,
                "status": task.status,
                "summary": None,
                "due_at": task.due_at.isoformat() if task.due_at else None,
                "source_refs": list(item.source_refs),
            }
        )
        source_refs.extend(item.source_refs)
    unique_refs = list(dict.fromkeys(source_refs))
    total = sum(len(values) for values in grouped.values())
    return {
        "period": {
            "start": scope.period.start_at.date().isoformat(),
            "end": scope.period.end_at.date().isoformat(),
        },
        "summary": (
            f"주간 Task {total}건: 완료 {len(grouped['completed'])}건, "
            f"진행 {len(grouped['in_progress'])}건, 지연 {len(grouped['delayed'])}건, "
            f"이슈 {len(grouped['issue'])}건."
        ),
        "completed": grouped["completed"],
        "in_progress": grouped["in_progress"],
        "delayed": grouped["delayed"],
        "unresolved_issues": grouped["issue"],
        "next_week_plans": [],
        "source_refs": unique_refs,
    }


def extract_next_week_plans(
    tasks: Iterable[TaskRecord],
    *,
    scope: WeeklyScope,
    explicit_plans: Iterable[ExplicitPlan] = (),
) -> tuple[dict[str, object], ...]:
    """다음 주 계획 후보를 원본 근거가 있는 경우에만 추출한다.

    다음 주 마감 Task는 `planned`, 미완료 Action Item은 `carry_over`로
    분류한다. 명시 계획 문장은 비어 있지 않은 `source_refs`가 있을 때만
    `planned`로 포함하며, 근거가 없는 문자열은 조용히 제외한다.
    """

    next_start = scope.period.end_at
    next_end = next_start + timedelta(days=7)
    candidates: list[dict[str, object]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for task in tasks:
        if task.assignee_user_id != scope.user_id:
            continue
        if task.deleted_at is not None or task.status in {"cancelled", "done"}:
            continue
        if not task.source_id and task.source_type != "manual":
            continue
        refs = _source_refs(task)
        kind: str | None = None
        if task.due_at is not None:
            if task.due_at.tzinfo is None or task.due_at.utcoffset() is None:
                raise ValueError("task.due_at must be timezone-aware")
            due_at = task.due_at.astimezone(next_start.tzinfo)
            if next_start <= due_at < next_end:
                kind = "planned"
        if kind is None and task.source_type == "action_item":
            kind = "carry_over"
        if kind is None:
            continue
        key = (task.title, refs)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({"title": task.title, "kind": kind, "source_refs": list(refs)})

    for plan in explicit_plans:
        title = plan.title.strip()
        refs = tuple(dict.fromkeys(ref for ref in plan.source_refs if ref))
        if not title or not refs:
            continue
        key = (title, refs)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({"title": title, "kind": "planned", "source_refs": list(refs)})

    candidates.sort(key=lambda item: (str(item["kind"]), str(item["title"]), tuple(item["source_refs"])))
    return tuple(candidates)


__all__ = [
    "WeeklyTaskItem",
    "build_weekly_report_data",
    "ExplicitPlan",
    "extract_next_week_plans",
    "link_action_item_tasks",
    "link_approved_tasks",
]
