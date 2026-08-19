"""M1.1-05 업무 Migration·Repository 통합 검수."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import unittest
from uuid import uuid4

from app.domain.sync_state import SyncStateRecord
from app.domain.task import TaskRecord
from app.repositories.sync_state import PostgresSyncStateRepository
from app.repositories.tasks import PostgresTaskRepository

TEST_DATABASE_URL = os.getenv("WORKMATE_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_DATABASE_URL, "WORKMATE_TEST_DATABASE_URL is not configured")
class M11PostgresIntegrationTests(unittest.TestCase):
    """업무 Migration의 재실행과 두 Repository의 권한 경계를 확인한다."""

    def test_migration_reexecution_scope_dedup_and_soft_delete(self) -> None:
        dsn = TEST_DATABASE_URL
        task_repository = PostgresTaskRepository(dsn)
        sync_repository = PostgresSyncStateRepository(dsn)
        # 두 Adapter가 같은 IF NOT EXISTS Migration을 다시 실행해도 실패하지 않아야 한다.
        task_repository._initialize()
        sync_repository._initialize()

        owner = str(uuid4())
        other = str(uuid4())
        source_id = f"integration-{uuid4()}"
        created = task_repository.create(
            TaskRecord(
                task_id=str(uuid4()),
                assignee_user_id=owner,
                title="통합 검수 Task",
                source_type="email",
                source_id=source_id,
            )
        )
        duplicate = task_repository.create(
            TaskRecord(
                task_id=str(uuid4()),
                assignee_user_id=owner,
                title="중복 원본",
                source_type="email",
                source_id=source_id,
            )
        )
        self.assertEqual(duplicate.task_id, created.task_id)
        self.assertIsNone(task_repository.get(created.task_id, other))
        self.assertTrue(task_repository.soft_delete(created.task_id, owner))
        self.assertIsNone(task_repository.get(created.task_id, owner))
        self.assertIsNotNone(task_repository.get(created.task_id, owner, include_deleted=True))

        sync_repository.upsert(
            SyncStateRecord(
                source_sync_state_id=str(uuid4()),
                sync_user_id=owner,
                source_type="gmail",
                sync_cursor="history-integration",
                last_synced_at=datetime.now(timezone.utc),
                status="succeeded",
            )
        )
        self.assertIsNotNone(sync_repository.get(owner, "gmail"))
        self.assertIsNone(sync_repository.get(other, "gmail"))


if __name__ == "__main__":
    unittest.main()
