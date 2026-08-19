"""M1.1-03 PostgreSQL 동기화 상태 실제 연결 검증."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import unittest
from uuid import uuid4

from app.domain.sync_state import SyncStateRecord
from app.repositories.sync_state import PostgresSyncStateRepository

TEST_DATABASE_URL = os.getenv("WORKMATE_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_DATABASE_URL, "WORKMATE_TEST_DATABASE_URL is not configured")
class PostgresSyncStateIntegrationTests(unittest.TestCase):
    """PostgreSQL에서 Cursor·Watch 상태의 사용자 격리와 Upsert를 확인한다."""

    def test_cursor_watch_upsert_and_scope(self) -> None:
        repository = PostgresSyncStateRepository(TEST_DATABASE_URL)
        user_id = str(uuid4())
        other_user_id = str(uuid4())
        saved = repository.upsert(
            SyncStateRecord(
                source_sync_state_id=str(uuid4()),
                sync_user_id=user_id,
                source_type="google_calendar",
                sync_cursor="calendar-token-1",
                channel_id="channel-1",
                channel_token="encrypted-test-token",
                resource_id="resource-1",
                watch_expiration=datetime(2026, 8, 20, tzinfo=timezone.utc),
                status="succeeded",
            )
        )
        self.assertEqual(repository.get(user_id, "google_calendar").source_sync_state_id, saved.source_sync_state_id)
        self.assertIsNone(repository.get(other_user_id, "google_calendar"))
        updated = repository.upsert(
            SyncStateRecord(
                source_sync_state_id=str(uuid4()),
                sync_user_id=user_id,
                source_type="google_calendar",
                sync_cursor="calendar-token-2",
                status="succeeded",
            )
        )
        self.assertEqual(updated.source_sync_state_id, saved.source_sync_state_id)
        self.assertEqual(repository.get(user_id, "google_calendar").sync_cursor, "calendar-token-2")


if __name__ == "__main__":
    unittest.main()
