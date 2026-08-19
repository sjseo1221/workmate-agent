"""M1.5-02 PrioritySnapshot 저장 경계 계약 테스트."""

from __future__ import annotations

from datetime import datetime, timezone
import unittest

from app.domain.priority import ScoreBreakdown
from app.domain.priority_snapshot import PrioritySnapshotRecord
from app.repositories.priority import SQLitePrioritySnapshotRepository


class PrioritySnapshotRepositoryTests(unittest.TestCase):
    """사용자 격리와 동일 계산 시점 멱등 저장을 검증한다."""

    def setUp(self) -> None:
        self.repository = SQLitePrioritySnapshotRepository()
        self.as_of = datetime(2026, 8, 17, 0, tzinfo=timezone.utc)
        self.breakdown = ScoreBreakdown(35, 15, 0, 0, 0)

    def snapshot(self, *, user_id: str = "user-a", task_id: str = "task-a", snapshot_id: str = "snapshot-a") -> PrioritySnapshotRecord:
        return PrioritySnapshotRecord(
            priority_snapshot_id=snapshot_id,
            recipient_user_id=user_id,
            ranked_task_id=task_id,
            calculated_at=self.as_of,
            as_of=self.as_of,
            rank=1,
            score=50,
            score_breakdown=self.breakdown,
            reasons=("오늘 마감",),
        )

    def test_save_and_read_are_user_scoped(self) -> None:
        self.repository.save_all("user-a", self.as_of, [self.snapshot()])
        self.repository.save_all("user-b", self.as_of, [self.snapshot(user_id="user-b", task_id="task-b", snapshot_id="snapshot-b")])
        self.assertEqual([item.ranked_task_id for item in self.repository.latest("user-a")], ["task-a"])
        self.assertEqual([item.ranked_task_id for item in self.repository.latest("user-b")], ["task-b"])

    def test_same_user_as_of_task_is_idempotent(self) -> None:
        self.repository.save_all("user-a", self.as_of, [self.snapshot()])
        updated = self.snapshot(snapshot_id="snapshot-new")
        self.repository.save_all("user-a", self.as_of, [updated])
        rows = self.repository.latest("user-a", as_of=self.as_of)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].priority_snapshot_id, "snapshot-new")

    def test_scope_mismatch_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.repository.save_all("user-a", self.as_of, [self.snapshot(user_id="user-b")])

    def test_invalid_score_breakdown_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            PrioritySnapshotRecord(
                priority_snapshot_id="snapshot-bad",
                recipient_user_id="user-a",
                ranked_task_id="task-a",
                calculated_at=self.as_of,
                as_of=self.as_of,
                rank=1,
                score=51,
                score_breakdown=self.breakdown,
                reasons=("오류",),
            )


if __name__ == "__main__":
    unittest.main()
