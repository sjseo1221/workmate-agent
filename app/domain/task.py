"""Task 업무 도메인 값 객체."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

TaskStatus = Literal["todo", "in_progress", "blocked", "done", "cancelled"]
TaskSourceType = Literal["manual", "action_item", "email", "calendar"]


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """사용자 범위와 원본 중복 규칙을 적용하는 업무 Task 모델."""

    task_id: str
    assignee_user_id: str
    title: str
    status: TaskStatus = "todo"
    priority_hint: int | None = None
    due_at: datetime | None = None
    source_type: TaskSourceType = "manual"
    source_id: str | None = None
    deleted_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        """Task 상태·출처 제약을 Repository보다 먼저 검증한다."""

        if not self.title.strip():
            raise ValueError("title must not be empty")
        if self.status not in {"todo", "in_progress", "blocked", "done", "cancelled"}:
            raise ValueError("unsupported task status")
        if self.source_type not in {"manual", "action_item", "email", "calendar"}:
            raise ValueError("unsupported task source type")
        if self.source_type == "manual" and self.source_id is not None:
            raise ValueError("manual task cannot have source_id")
        if self.source_type != "manual" and not self.source_id:
            raise ValueError("external task requires source_id")
        if self.priority_hint is not None and not 0 <= self.priority_hint <= 10:
            raise ValueError("priority_hint must be between 0 and 10")
