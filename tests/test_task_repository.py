"""M1.1-02 Task Repository 경계 계약 테스트."""

from __future__ import annotations

from datetime import datetime, timezone
import unittest
from uuid import uuid4

from app.domain.task import TaskRecord
from app.repositories.tasks import SQLiteTaskRepository


class TaskRepositoryTests(unittest.TestCase):
    """업무 Task의 사용자 범위·중복·Soft Delete 불변식을 검증한다."""

    def setUp(self) -> None:
        self.repository = SQLiteTaskRepository()
        self.user_id = "user-a"
        self.other_user_id = "user-b"

    def _task(self, *, user_id: str | None = None, source_id: str | None = None) -> TaskRecord:
        return TaskRecord(
            task_id=str(uuid4()),
            assignee_user_id=user_id or self.user_id,
            title="보고서 초안 작성",
            due_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
            source_type="email" if source_id else "manual",
            source_id=source_id,
        )

    def test_create_and_list_are_scoped_to_user(self) -> None:
        own = self.repository.create(self._task())
        other = self.repository.create(self._task(user_id=self.other_user_id))
        self.assertEqual([item.task_id for item in self.repository.list(self.user_id)], [own.task_id])
        self.assertIsNone(self.repository.get(other.task_id, self.user_id))

    def test_same_external_source_returns_existing_task(self) -> None:
        first = self.repository.create(self._task(source_id="gmail-message-1"))
        duplicate = self.repository.create(self._task(source_id="gmail-message-1"))
        self.assertEqual(duplicate.task_id, first.task_id)
        self.assertEqual(len(self.repository.list(self.user_id)), 1)

    def test_soft_delete_hides_task_but_keeps_record(self) -> None:
        task = self.repository.create(self._task())
        self.assertTrue(self.repository.soft_delete(task.task_id, self.user_id))
        self.assertIsNone(self.repository.get(task.task_id, self.user_id))
        deleted = self.repository.get(task.task_id, self.user_id, include_deleted=True)
        self.assertIsNotNone(deleted)
        self.assertIsNotNone(deleted.deleted_at)

    def test_update_cannot_cross_user_boundary(self) -> None:
        task = self.repository.create(self._task())
        self.assertIsNone(self.repository.update(task.task_id, self.other_user_id, title="변경"))
        self.assertEqual(self.repository.get(task.task_id, self.user_id).title, "보고서 초안 작성")

    def test_list_order_is_most_recently_created_first(self) -> None:
        """목록은 등록일시(생성 시각) 최신순으로 반환해야 한다(2026-08-15 결정).

        이전 버전은 `updated_at DESC`로 정렬했고, 이 테스트도 두 Task를 연속
        생성해 `created_at`이 우연히 같을 때만 성립하는 `task_id` 오름차순
        가정으로 작성돼 있어 실행 환경에 따라 간헐적으로 실패했다(Flaky).
        실제로 서로 다른 생성 시각을 명시적으로 주입해 "최신 생성이 먼저
        나온다"는 진짜 요구사항을 검증하도록 고쳤다.
        """

        earlier = self.repository.create(
            TaskRecord(
                task_id=str(uuid4()),
                assignee_user_id=self.user_id,
                title="먼저 등록한 Task",
                created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
        )
        later = self.repository.create(
            TaskRecord(
                task_id=str(uuid4()),
                assignee_user_id=self.user_id,
                title="나중에 등록한 Task",
                created_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
            )
        )
        listed = self.repository.list(self.user_id)
        self.assertEqual([item.task_id for item in listed], [later.task_id, earlier.task_id])


if __name__ == "__main__":
    unittest.main()
