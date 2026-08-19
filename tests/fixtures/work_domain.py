"""M1.1 업무 도메인 테스트 Fixture.

운영 코드나 운영 DB 초기화에는 import하지 않는다. 모든 값은 계약 테스트의
사용자 격리·중복·Soft Delete 시나리오를 재현하기 위한 비밀정보 없는 값이다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from app.domain.sync_state import SyncStateRecord
from app.domain.task import TaskRecord


FIXTURE_TIME = datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc)
OWNER_USER_ID = "00000000-0000-0000-0000-000000000001"
OTHER_USER_ID = "00000000-0000-0000-0000-000000000002"


def task_fixture(*, task_id: str, user_id: str = OWNER_USER_ID, source_id: str | None = None, deleted: bool = False) -> TaskRecord:
    """정상·외부 원본·Soft Delete Task를 만든다."""

    UUID(task_id)
    return TaskRecord(
        task_id=task_id,
        assignee_user_id=user_id,
        title="주간 보고서 초안 작성",
        source_type="email" if source_id else "manual",
        source_id=source_id,
        deleted_at=FIXTURE_TIME if deleted else None,
        created_at=FIXTURE_TIME,
        updated_at=FIXTURE_TIME,
    )


def sync_state_fixture(*, source_type: str = "gmail", user_id: str = OWNER_USER_ID) -> SyncStateRecord:
    """Gmail 또는 Calendar 증분 Cursor 상태를 만든다."""

    UUID(user_id)
    if source_type == "gmail":
        cursor = "history-fixture-001"
    elif source_type == "google_calendar":
        cursor = "calendar-fixture-001"
    else:
        raise ValueError("unsupported fixture source type")
    return SyncStateRecord(
        source_sync_state_id="00000000-0000-0000-0000-000000000101",
        sync_user_id=user_id,
        source_type=source_type,  # type: ignore[arg-type]
        sync_cursor=cursor,
        channel_id="channel-fixture-001",
        resource_id="resource-fixture-001",
        watch_expiration=FIXTURE_TIME,
        last_synced_at=FIXTURE_TIME,
        status="succeeded",
        created_at=FIXTURE_TIME,
        updated_at=FIXTURE_TIME,
    )
