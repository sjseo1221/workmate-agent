"""M1.1-03 외부 동기화 상태 Repository 계약 테스트."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import unittest
from uuid import uuid4

from app.domain.sync_state import SyncStateRecord
from app.repositories.sync_state import SQLiteSyncStateRepository


class SyncStateRepositoryTests(unittest.TestCase):
    """Cursor·Watch·실패 상태가 사용자별로 격리되는지 검증한다."""

    def setUp(self) -> None:
        self.repository = SQLiteSyncStateRepository()
        self.user_id = "user-a"
        self.other_user_id = "user-b"

    def _state(self, *, user_id: str | None = None, source: str = "gmail", status: str = "succeeded", error: str | None = None) -> SyncStateRecord:
        return SyncStateRecord(
            source_sync_state_id=str(uuid4()),
            sync_user_id=user_id or self.user_id,
            source_type=source,  # type: ignore[arg-type]
            sync_cursor="history-123" if source == "gmail" else "calendar-token-123",
            channel_id="channel-1",
            channel_token="encrypted-token",
            resource_id="resource-1",
            watch_expiration=datetime(2026, 8, 20, tzinfo=timezone.utc),
            last_synced_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
            status=status,  # type: ignore[arg-type]
            error_message=error,
        )

    def test_upsert_updates_same_user_source(self) -> None:
        first = self.repository.upsert(self._state())
        updated = replace(self._state(), source_sync_state_id=str(uuid4()), sync_cursor="history-456")
        saved = self.repository.upsert(updated)
        self.assertEqual(saved.sync_cursor, "history-456")
        self.assertEqual(len(self.repository.list(self.user_id)), 1)
        self.assertEqual(saved.sync_user_id, first.sync_user_id)

    def test_get_and_list_are_scoped_to_user(self) -> None:
        own = self.repository.upsert(self._state())
        other = self.repository.upsert(self._state(user_id=self.other_user_id))
        self.assertEqual(self.repository.get(self.user_id, "gmail").source_sync_state_id, own.source_sync_state_id)
        self.assertIsNone(self.repository.get(self.user_id, "google_calendar"))
        self.assertEqual(len(self.repository.list(self.user_id)), 1)
        self.assertNotEqual(other.sync_user_id, self.user_id)

    def test_calendar_cursor_and_watch_fields_round_trip(self) -> None:
        saved = self.repository.upsert(self._state(source="google_calendar"))
        restored = self.repository.get(self.user_id, "google_calendar")
        self.assertIsNotNone(restored)
        self.assertEqual(restored.sync_cursor, saved.sync_cursor)
        self.assertEqual(restored.channel_id, "channel-1")
        self.assertEqual(restored.resource_id, "resource-1")

    def test_failed_state_requires_and_preserves_error(self) -> None:
        saved = self.repository.upsert(self._state(status="failed", error="410 Gone"))
        self.assertEqual(self.repository.get(self.user_id, "gmail").error_message, "410 Gone")
        self.assertEqual(saved.status, "failed")


if __name__ == "__main__":
    unittest.main()
