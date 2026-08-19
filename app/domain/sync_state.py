"""외부 소스 증분 동기화 상태 도메인 모델."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

SyncSourceType = Literal["gmail", "google_calendar"]
SyncStatus = Literal["idle", "running", "succeeded", "failed"]


@dataclass(frozen=True, slots=True)
class SyncStateRecord:
    """사용자별 Gmail·Calendar Cursor와 Watch 상태를 표현한다."""

    source_sync_state_id: str
    sync_user_id: str
    source_type: SyncSourceType
    sync_cursor: str | None = None
    channel_id: str | None = None
    channel_token: str | None = None
    resource_id: str | None = None
    watch_expiration: datetime | None = None
    last_synced_at: datetime | None = None
    status: SyncStatus = "idle"
    error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        """소스와 동기화 상태의 허용된 값만 통과시킨다."""

        if self.source_type not in {"gmail", "google_calendar"}:
            raise ValueError("unsupported sync source type")
        if self.status not in {"idle", "running", "succeeded", "failed"}:
            raise ValueError("unsupported sync status")
        if self.status == "failed" and not self.error_message:
            raise ValueError("failed sync requires error_message")
