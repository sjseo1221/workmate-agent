"""M1.1-04 업무 도메인 Fixture 계약 테스트."""

from __future__ import annotations

import unittest

from app.domain.sync_state import SyncStateRecord
from app.domain.task import TaskRecord
from tests.fixtures.work_domain import (
    OTHER_USER_ID,
    OWNER_USER_ID,
    sync_state_fixture,
    task_fixture,
)


class WorkDomainFixtureTests(unittest.TestCase):
    """Fixture가 실제 운영 경로가 아닌 테스트 입력으로만 유효한지 확인한다."""

    def test_normal_task_fixture(self) -> None:
        task = task_fixture(task_id="00000000-0000-0000-0000-000000000201")
        self.assertIsInstance(task, TaskRecord)
        self.assertEqual(task.assignee_user_id, OWNER_USER_ID)
        self.assertIsNone(task.deleted_at)

    def test_soft_deleted_task_fixture(self) -> None:
        task = task_fixture(task_id="00000000-0000-0000-0000-000000000202", deleted=True)
        self.assertIsNotNone(task.deleted_at)

    def test_duplicate_external_source_fixture(self) -> None:
        first = task_fixture(task_id="00000000-0000-0000-0000-000000000203", source_id="gmail-fixture-001")
        duplicate = task_fixture(task_id="00000000-0000-0000-0000-000000000204", source_id="gmail-fixture-001")
        self.assertEqual(first.source_id, duplicate.source_id)
        self.assertNotEqual(first.task_id, duplicate.task_id)

    def test_user_isolation_fixture(self) -> None:
        own = task_fixture(task_id="00000000-0000-0000-0000-000000000205")
        other = task_fixture(task_id="00000000-0000-0000-0000-000000000206", user_id=OTHER_USER_ID)
        self.assertNotEqual(own.assignee_user_id, other.assignee_user_id)

    def test_provider_cursor_fixtures(self) -> None:
        gmail = sync_state_fixture(source_type="gmail")
        calendar = sync_state_fixture(source_type="google_calendar")
        self.assertIsInstance(gmail, SyncStateRecord)
        self.assertTrue(gmail.sync_cursor.startswith("history-"))
        self.assertTrue(calendar.sync_cursor.startswith("calendar-"))


if __name__ == "__main__":
    unittest.main()
