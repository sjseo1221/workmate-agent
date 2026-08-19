"""M1.5-01 우선순위 Domain 계약 테스트."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from app.domain.priority import rank_tasks, score_task
from app.domain.task import TaskRecord


class PriorityDomainTests(unittest.TestCase):
    """점수식·timezone·상태 제외·동점 정렬의 결정성을 검증한다."""

    as_of = datetime(2026, 8, 17, 9, tzinfo=timezone(timedelta(hours=9)))

    def task(self, **kwargs: object) -> TaskRecord:
        return TaskRecord(
            task_id=kwargs.pop("task_id", str(uuid4())),
            assignee_user_id="user-a",
            title=kwargs.pop("title", "업무"),
            priority_hint=kwargs.pop("priority_hint", None),
            due_at=kwargs.pop("due_at", None),
            status=kwargs.pop("status", "todo"),
            source_type=kwargs.pop("source_type", "manual"),
            source_id=kwargs.pop("source_id", None),
            updated_at=kwargs.pop("updated_at", self.as_of),
            **kwargs,
        )

    def test_score_uses_local_date_and_hint_mapping(self) -> None:
        task = self.task(
            due_at=datetime(2026, 8, 18, 0, 30, tzinfo=timezone.utc),
            priority_hint=8,
        )
        total, breakdown, reasons = score_task(task, as_of=self.as_of, timezone_name="Asia/Seoul")
        self.assertEqual(breakdown.deadline, 30)
        self.assertEqual(breakdown.importance, 20)
        self.assertEqual(total, 50)
        self.assertIn("마감 임박", reasons)

    def test_blocked_caps_overdue_component_at_15(self) -> None:
        task = self.task(
            status="blocked",
            due_at=self.as_of - timedelta(days=1),
        )
        _, breakdown, _ = score_task(task, as_of=self.as_of, timezone_name="Asia/Seoul")
        self.assertEqual(breakdown.blocked_or_overdue, 15)

    def test_rank_excludes_done_cancelled_and_deleted(self) -> None:
        visible = self.task(task_id="a", priority_hint=8)
        result = rank_tasks(
            [visible, self.task(task_id="b", status="done"), self.task(task_id="c", status="cancelled")],
            as_of=self.as_of,
            timezone_name="Asia/Seoul",
        )
        self.assertEqual([item.task.task_id for item in result], ["a"])

    def test_tie_breaks_by_due_updated_then_id(self) -> None:
        due = self.as_of + timedelta(days=2)
        tasks = [
            self.task(task_id="b", due_at=due, priority_hint=8),
            self.task(task_id="a", due_at=due, priority_hint=8),
        ]
        result = rank_tasks(tasks, as_of=self.as_of, timezone_name="Asia/Seoul")
        self.assertEqual([item.task.task_id for item in result], ["a", "b"])

    def test_action_item_gets_meeting_commitment(self) -> None:
        task = self.task(source_type="action_item", source_id="action-1")
        result = rank_tasks([task], as_of=self.as_of, timezone_name="Asia/Seoul")
        self.assertEqual(result[0].breakdown.meeting_commitment, 10)

    def test_calendar_task_within_24_hours_gets_full_calendar_relevance(self) -> None:
        """03 §6 "일정 연관"(24시간 내 10) — Calendar 제안에서 승인된 Task만
        대상이다(2026-08-17, 실사용 중 "일정 연관 0"이 이상하다는 문의로 발견 —
        `rank_tasks`가 `calendar_relevance`를 계산해 넘긴 적이 아예 없었다)."""

        task = self.task(source_type="calendar", source_id="primary:event-1", due_at=self.as_of + timedelta(hours=6))
        result = rank_tasks([task], as_of=self.as_of, timezone_name="Asia/Seoul")
        self.assertEqual(result[0].breakdown.calendar_relevance, 10)
        self.assertIn("관련 일정 임박", result[0].reasons)

    def test_calendar_task_within_3_days_gets_partial_calendar_relevance(self) -> None:
        task = self.task(source_type="calendar", source_id="primary:event-2", due_at=self.as_of + timedelta(days=2))
        result = rank_tasks([task], as_of=self.as_of, timezone_name="Asia/Seoul")
        self.assertEqual(result[0].breakdown.calendar_relevance, 5)

    def test_calendar_task_more_than_3_days_away_gets_no_calendar_relevance(self) -> None:
        task = self.task(source_type="calendar", source_id="primary:event-3", due_at=self.as_of + timedelta(days=10))
        result = rank_tasks([task], as_of=self.as_of, timezone_name="Asia/Seoul")
        self.assertEqual(result[0].breakdown.calendar_relevance, 0)

    def test_calendar_relevance_also_counts_a_recently_passed_event(self) -> None:
        """"임박"의 방향을 미래로만 좁히지 않는다 — 이미 시작한 일정도 아직
        처리 안 됐다면 여전히 "관련"으로 본다(거리의 절댓값만 본다)."""

        task = self.task(source_type="calendar", source_id="primary:event-4", due_at=self.as_of - timedelta(hours=3))
        result = rank_tasks([task], as_of=self.as_of, timezone_name="Asia/Seoul")
        self.assertEqual(result[0].breakdown.calendar_relevance, 10)

    def test_calendar_relevance_is_zero_without_a_due_at_or_for_non_calendar_tasks(self) -> None:
        no_due_at = self.task(source_type="calendar", source_id="primary:event-5")
        manual_but_soon = self.task(source_type="manual", due_at=self.as_of + timedelta(hours=1))
        for task in (no_due_at, manual_but_soon):
            result = rank_tasks([task], as_of=self.as_of, timezone_name="Asia/Seoul")
            self.assertEqual(result[0].breakdown.calendar_relevance, 0)

    def test_invalid_timezone_and_naive_as_of_are_rejected(self) -> None:
        task = self.task()
        with self.assertRaises(ValueError):
            score_task(task, as_of=self.as_of, timezone_name="Not/AZone")
        with self.assertRaises(ValueError):
            score_task(task, as_of=self.as_of.replace(tzinfo=None), timezone_name="Asia/Seoul")


if __name__ == "__main__":
    unittest.main()
