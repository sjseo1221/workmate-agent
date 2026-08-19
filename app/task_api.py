"""Task 업무 API와 Repository 연결 경계.

공개 API는 요청 사용자의 `user_id` 범위 안에서만 Task를 읽고 변경한다.
Repository 선택은 운영의 `DATABASE_URL`과 로컬 테스트용 SQLite 설정으로
분리하며, API Route에는 업무 규칙을 두지 않고 Domain/Repository로 전달한다.
"""

from __future__ import annotations

import os
from datetime import datetime
from functools import lru_cache
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.domain.task import TaskRecord, TaskSourceType, TaskStatus
from app.internal_chat import _authenticated_user_or_assignee
from app.meeting_api import meeting_repository as _meeting_repository
from app.repositories.tasks import (
    PostgresTaskRepository,
    SQLiteTaskRepository,
    TaskRepository,
)


TASK_DB_PATH_ENV = "WORKMATE_TASK_DB_PATH"
DATABASE_URL_ENV = "DATABASE_URL"


class TaskCreateRequest(BaseModel):
    """사용자가 등록할 Task 입력이다."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    status: TaskStatus = "todo"
    priority_hint: int | None = Field(default=None, ge=0, le=10)
    due_at: datetime | None = None
    source_type: TaskSourceType = "manual"
    source_id: str | None = Field(default=None, max_length=500)


class TaskUpdateRequest(BaseModel):
    """Task 수정 입력이다. 전달된 필드만 변경한다."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=500)
    status: TaskStatus | None = None
    priority_hint: int | None = Field(default=None, ge=0, le=10)
    due_at: datetime | None = None


class TaskMeetingEvidence(BaseModel):
    """승인된 Action Item Task의 회의록 근거 — 읽기 전용(15번 문서 회의 관리 #4·
    할 일 관리, 2026-08-16, 17번 갭 문서 #11)."""

    meeting_id: str
    meeting_title: str
    action_item_id: str
    evidence_text: str
    meeting_chunk_id: str | None


class TaskResponse(BaseModel):
    """Task 저장 결과를 API JSON으로 직렬화한다."""

    model_config = ConfigDict(from_attributes=True)

    task_id: str
    assignee_user_id: str
    title: str
    status: TaskStatus
    priority_hint: int | None
    due_at: datetime | None
    source_type: TaskSourceType
    source_id: str | None
    deleted_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None
    meeting_evidence: TaskMeetingEvidence | None = None


def _meeting_evidence(task: TaskRecord) -> TaskMeetingEvidence | None:
    """`source_type="action_item"` Task의 근거를 회의 Repository에서 되짚는다.

    `source_id`는 `{meeting_id}:{action_item_id}` 형태다(2026-08-16, 17번 갭
    문서 #11 — `app/action_item_api.py`의 `_source_id`와 짝을 이룬다). 이 형식이
    아니면(형식이 바뀌기 전에 만들어진 Task 등) 조용히 None을 반환한다 —
    근거 표시는 부가 정보라 Task 조회 자체를 막을 이유가 없다."""

    if task.source_type != "action_item" or not task.source_id or ":" not in task.source_id:
        return None
    meeting_id, _, action_item_id = task.source_id.partition(":")
    # 회의를 삭제해도(Soft Delete) 이 Task의 근거 추적은 계속 살아 있어야 한다
    # (2026-08-17, 사용자 요청) — `include_deleted=True`로 찾는다.
    meeting = _meeting_repository().get(meeting_id, task.assignee_user_id, include_deleted=True)
    if meeting is None:
        return None
    action = next(
        (row for row in _meeting_repository().list_actions(meeting_id, task.assignee_user_id) if row["action_item_id"] == action_item_id),
        None,
    )
    if action is None:
        return None
    return TaskMeetingEvidence(
        meeting_id=meeting_id,
        meeting_title=meeting.title,
        action_item_id=action_item_id,
        evidence_text=str(action["evidence_text"]),
        meeting_chunk_id=action["meeting_chunk_id"],  # type: ignore[arg-type]
    )


def _task_response(task: TaskRecord) -> TaskResponse:
    """Domain Task를 공개 응답 모델로 변환하고 회의 근거를 덧붙인다."""

    response = TaskResponse.model_validate(task)
    return response.model_copy(update={"meeting_evidence": _meeting_evidence(task)})


@lru_cache(maxsize=1)
def task_repository() -> TaskRepository:
    """환경 설정에 맞는 단일 Task Repository를 반환한다."""

    database_url = os.getenv(DATABASE_URL_ENV)
    if database_url:
        return PostgresTaskRepository(database_url)
    return SQLiteTaskRepository(os.getenv(TASK_DB_PATH_ENV, ".runtime/tasks.sqlite3"))


def _validate_source(source_type: TaskSourceType, source_id: str | None) -> None:
    """수동·외부 원본 Task의 불변식을 API 경계에서 검증한다."""

    if source_type == "manual" and source_id is not None:
        raise HTTPException(status_code=422, detail="manual task cannot have source_id")
    if source_type != "manual" and not source_id:
        raise HTTPException(status_code=422, detail="external task requires source_id")


router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskResponse])
def list_tasks(
    status_filter: TaskStatus | None = Query(default=None, alias="status"),
    due_before: datetime | None = Query(default=None),
    include_deleted: bool = False,
    user_id: str = Depends(_authenticated_user_or_assignee),
) -> list[TaskResponse]:
    """요청 사용자의 삭제되지 않은 Task 목록을 결정적 순서로 반환한다.

    `assignee_user_id` 필터는 두지 않는다 — 모든 Task가 이미 요청 사용자
    본인 범위로만 조회돼 값이 항상 하나뿐이라 필터로서 의미가 없다
    (2026-08-15 결정, `14-workmate-ui-integration-gaps.md` #6).
    """

    tasks = task_repository().list(user_id, include_deleted=include_deleted)
    if status_filter is not None:
        tasks = [task for task in tasks if task.status == status_filter]
    if due_before is not None:
        tasks = [task for task in tasks if task.due_at is not None and task.due_at <= due_before]
    return [_task_response(task) for task in tasks]


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
def create_task(
    payload: TaskCreateRequest,
    user_id: str = Depends(_authenticated_user_or_assignee),
) -> TaskResponse:
    """Task를 생성하고 외부 원본 중복이면 기존 Task를 멱등 반환한다."""

    _validate_source(payload.source_type, payload.source_id)
    task = TaskRecord(
        task_id=str(uuid4()),
        assignee_user_id=user_id,
        title=payload.title,
        status=payload.status,
        priority_hint=payload.priority_hint,
        due_at=payload.due_at,
        source_type=payload.source_type,
        source_id=payload.source_id,
    )
    return _task_response(task_repository().create(task))


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(task_id: str, user_id: str = Depends(_authenticated_user_or_assignee)) -> TaskResponse:
    """요청 사용자가 소유한 Task만 조회한다."""

    task = task_repository().get(task_id, user_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return _task_response(task)


@router.patch("/{task_id}", response_model=TaskResponse)
def update_task(
    task_id: str,
    payload: TaskUpdateRequest,
    user_id: str = Depends(_authenticated_user_or_assignee),
) -> TaskResponse:
    """요청 사용자의 활성 Task 필드만 수정한다."""

    if payload.title is not None and not payload.title.strip():
        raise HTTPException(status_code=422, detail="title must not be blank")
    task = task_repository().update(
        task_id,
        user_id,
        title=payload.title,
        status=payload.status,
        priority_hint=payload.priority_hint,
        due_at=payload.due_at,
    )
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return _task_response(task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def delete_task(task_id: str, user_id: str = Depends(_authenticated_user_or_assignee)) -> None:
    """Task를 물리 삭제하지 않고 soft delete한다."""

    if not task_repository().soft_delete(task_id, user_id):
        raise HTTPException(status_code=404, detail="task not found")


__all__ = [
    "DATABASE_URL_ENV",
    "TASK_DB_PATH_ENV",
    "TaskCreateRequest",
    "TaskUpdateRequest",
    "TaskResponse",
    "router",
    "task_repository",
]
