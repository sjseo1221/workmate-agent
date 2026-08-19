"""M1.5-03 결정적 우선순위 Workflow."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from functools import lru_cache
from typing import Any
from uuid import uuid4

from app.domain.priority import rank_tasks
from app.domain.priority_snapshot import PrioritySnapshotRecord
from app.repositories.priority import (
    PostgresPrioritySnapshotRepository,
    PrioritySnapshotRepository,
    SQLitePrioritySnapshotRepository,
)
from app.repositories.tasks import PostgresTaskRepository, SQLiteTaskRepository, TaskRepository
from app.workflows.registry import WorkflowRequest, WorkflowResult


TASK_DB_PATH_ENV = "WORKMATE_TASK_DB_PATH"
DATABASE_URL_ENV = "DATABASE_URL"


def _as_of(payload: dict[str, object]) -> datetime:
    """입력의 기준 시각을 파싱하고 없으면 UTC 현재 시각을 사용한다."""

    value = payload.get("as_of")
    if isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return parsed
    return datetime.now(timezone.utc)


def _timezone(payload: dict[str, object]) -> str:
    """사용자 입력의 IANA timezone을 반환하고 기본값을 적용한다."""

    value = payload.get("timezone", "Asia/Seoul")
    if not isinstance(value, str) or not value:
        raise ValueError("timezone must be a non-empty string")
    return value


def _changes(previous: list[PrioritySnapshotRecord], current: list[PrioritySnapshotRecord]) -> list[dict[str, object]]:
    """이전 Top 3와 현재 Top 3의 순위 변화를 결정적으로 계산한다."""

    before = {item.ranked_task_id: item.rank for item in previous}
    after = {item.ranked_task_id: item.rank for item in current}
    task_ids = sorted(set(before) | set(after))
    changes: list[dict[str, object]] = []
    for task_id in task_ids:
        old_rank = before.get(task_id)
        new_rank = after.get(task_id)
        if old_rank is None:
            change_type, reason = "entered", "새로 Top 3에 진입"
        elif new_rank is None:
            change_type, reason = "removed", "Top 3에서 제외"
        elif new_rank < old_rank:
            change_type, reason = "moved_up", f"순위 {old_rank}위에서 {new_rank}위로 상승"
        elif new_rank > old_rank:
            change_type, reason = "moved_down", f"순위 {old_rank}위에서 {new_rank}위로 하락"
        else:
            change_type, reason = "unchanged", "순위 변동 없음"
        changes.append(
            {
                "task_id": task_id,
                "change_type": change_type,
                "previous_rank": old_rank,
                "current_rank": new_rank,
                "reason": reason,
            }
        )
    return changes


def _repositories() -> tuple[TaskRepository, PrioritySnapshotRepository]:
    """운영 DSN 또는 로컬 SQLite에 연결된 두 Repository를 생성한다."""

    database_url = os.getenv(DATABASE_URL_ENV)
    if database_url:
        return PostgresTaskRepository(database_url), PostgresPrioritySnapshotRepository(database_url)
    path = os.getenv(TASK_DB_PATH_ENV, ".runtime/tasks.sqlite3")
    return SQLiteTaskRepository(path), SQLitePrioritySnapshotRepository(path)


def build_rank_priorities_workflow() -> Any:
    """`rank_priorities`용 Workflow 핸들러를 반환한다."""

    task_repository, snapshot_repository = _repositories()

    async def execute(request: WorkflowRequest) -> WorkflowResult:
        """사용자 소유 Task만 읽어 결정적 Top 3와 Snapshot을 반환한다."""

        payload = dict(request.payload)
        as_of = _as_of(payload)
        timezone_name = _timezone(payload)
        previous = snapshot_repository.latest(request.user_id, limit=3)
        ranked = rank_tasks(
            task_repository.list(request.user_id),
            as_of=as_of,
            timezone_name=timezone_name,
            top_n=3,
        )
        calculated_at = datetime.now(timezone.utc)
        snapshots = [
            PrioritySnapshotRecord(
                priority_snapshot_id=str(uuid4()),
                recipient_user_id=request.user_id,
                ranked_task_id=item.task.task_id,
                calculated_at=calculated_at,
                as_of=as_of,
                rank=item.rank,
                score=item.score,
                score_breakdown=item.breakdown,
                reasons=item.reasons,
            )
            for item in ranked
        ]
        snapshot_repository.save_all(request.user_id, as_of, snapshots)
        data = {
            "calculated_at": calculated_at.isoformat(),
            "priorities": [
                {
                    "rank": item.rank,
                    "task_id": item.task.task_id,
                    "title": item.task.title,
                    "score": item.score,
                    "score_breakdown": {
                        "deadline": item.breakdown.deadline,
                        "importance": item.breakdown.importance,
                        "blocked_or_overdue": item.breakdown.blocked_or_overdue,
                        "meeting_commitment": item.breakdown.meeting_commitment,
                        "calendar_relevance": item.breakdown.calendar_relevance,
                    },
                    "reasons": list(item.reasons),
                }
                for item in ranked
            ],
            "changes": _changes(previous, snapshots),
            "source_refs": [item.task.task_id for item in ranked],
        }
        result_payload = {"type": "priority_ranking", "data": data}
        return WorkflowResult(
            artifact_name="priority_ranking",
            artifact_description="Deterministic Top 3 ranking computed from user-owned Tasks.",
            text=json.dumps(result_payload, ensure_ascii=False, sort_keys=True),
            # `analyze_meeting`·`search_meetings`·`weekly_report`와 같은 이유로 `data=`를
            # 채운다 — 비어 있으면 프론트가 항상 `null`만 읽는다(daily_briefing.py 주석
            # 참고, 2026-08-16, 14번 갭 문서).
            data=result_payload,
            mock=False,
        )

    return execute


__all__ = ["build_rank_priorities_workflow"]
