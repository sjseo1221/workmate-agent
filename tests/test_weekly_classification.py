"""M2.1-01 주간 기간·사용자 범위 정규화 계약 테스트."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import unittest

from app.domain.task import TaskRecord
from app.domain.weekly import classify_task_status, normalize_week_scope


class WeeklyScopeTests(unittest.TestCase):
    """주간 경계, DST, timezone과 사용자 범위를 검증한다."""

    def test_iso_date_creates_local_half_open_seven_day_period(self) -> None:
        scope = normalize_week_scope(
            " user-1 ",
            "2026-08-17",
            "Asia/Seoul",
            as_of="2026-08-18T03:00:00+00:00",
        )

        self.assertEqual(scope.user_id, "user-1")
        self.assertEqual(scope.period.week_of, date(2026, 8, 17))
        self.assertEqual(scope.period.start_at.isoformat(), "2026-08-17T00:00:00+09:00")
        self.assertEqual(scope.period.end_at.isoformat(), "2026-08-24T00:00:00+09:00")
        self.assertEqual(scope.as_of.isoformat(), "2026-08-18T12:00:00+09:00")

    def test_dst_keeps_local_calendar_boundaries(self) -> None:
        scope = normalize_week_scope(
            "user-1",
            date(2026, 3, 8),
            "America/Los_Angeles",
            as_of=datetime(2026, 3, 9, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(scope.period.start_at.isoformat(), "2026-03-08T00:00:00-08:00")
        self.assertEqual(scope.period.end_at.isoformat(), "2026-03-15T00:00:00-07:00")

    def test_full_iso_datetime_string_is_accepted_and_reduced_to_its_date(self) -> None:
        """실사용 중 발견한 버그(2026-08-17)의 회귀 테스트 — assistant_ask
        Router가 채우는 `week_of`가 가끔 순수 `YYYY-MM-DD`가 아니라 시각·
        timezone까지 붙은 전체 ISO 8601 문자열로 나왔다("2026-08-10T00:00:00+09:00")
        — 이전엔 `date.fromisoformat()`이 바로 실패해 "week_of must be an ISO
        date" 오류로 주간보고 자체가 실패했다. 날짜 부분만 뽑아 정상 처리하는지
        확인한다."""

        scope = normalize_week_scope("user-1", "2026-08-10T00:00:00+09:00", "Asia/Seoul")
        self.assertEqual(scope.period.week_of, date(2026, 8, 10))

    def test_invalid_scope_values_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_week_scope("", "2026-08-17", "Asia/Seoul")
        with self.assertRaises(ValueError):
            normalize_week_scope("user-1", "2026-02-30", "Asia/Seoul")
        with self.assertRaises(ValueError):
            normalize_week_scope("user-1", "2026-08-17", "Not/AZone")
        with self.assertRaises(ValueError):
            normalize_week_scope("user-1", "2026-08-17", "Asia/Seoul", as_of="2026-08-17T00:00:00")


class WeeklyTaskClassificationTests(unittest.TestCase):
    """Task 상태와 Soft Delete가 한 가지 주간 bucket으로 분류되는지 검증한다."""

    as_of = datetime(2026, 8, 17, 9, tzinfo=timezone(timedelta(hours=9)))

    def task(self, **kwargs: object) -> TaskRecord:
        return TaskRecord(
            task_id=str(kwargs.pop("task_id", "task-1")),
            assignee_user_id="user-1",
            title="업무",
            status=kwargs.pop("status", "todo"),
            due_at=kwargs.pop("due_at", None),
            deleted_at=kwargs.pop("deleted_at", None),
            source_type="manual",
        )

    def test_buckets_are_mutually_exclusive(self) -> None:
        self.assertEqual(classify_task_status(self.task(status="done"), as_of=self.as_of), "completed")
        self.assertEqual(classify_task_status(self.task(status="in_progress"), as_of=self.as_of), "in_progress")
        self.assertEqual(classify_task_status(self.task(status="blocked"), as_of=self.as_of), "issue")
        self.assertEqual(
            classify_task_status(self.task(due_at=self.as_of - timedelta(minutes=1)), as_of=self.as_of),
            "delayed",
        )
        self.assertEqual(classify_task_status(self.task(status="cancelled"), as_of=self.as_of), "excluded")
        self.assertEqual(classify_task_status(self.task(deleted_at=self.as_of), as_of=self.as_of), "excluded")

    def test_blocked_and_overdue_is_issue_not_delayed(self) -> None:
        """`blocked`이면서 기한도 지났으면 막힌 원인 해소가 우선이라 `issue`로
        남는다 — `delayed`로 다시 떨어지지 않는다(2026-08-16, 17번 갭 문서 #16)."""

        self.assertEqual(
            classify_task_status(self.task(status="blocked", due_at=self.as_of - timedelta(days=3)), as_of=self.as_of),
            "issue",
        )

    def test_invalid_clock_values_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            classify_task_status(self.task(), as_of=self.as_of.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            classify_task_status(self.task(due_at=datetime(2026, 8, 17, 8)), as_of=self.as_of)


if __name__ == "__main__":
    unittest.main()
