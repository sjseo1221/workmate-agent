"""M2.1-03 Gmail·Calendar 승인 Task 원본 연결 검증."""

from datetime import datetime, timezone
import unittest

from app.domain.task import TaskRecord
from app.domain.weekly import normalize_week_scope
from app.workflows.weekly_classification import (
    ExplicitPlan,
    build_weekly_report_data,
    extract_next_week_plans,
    link_action_item_tasks,
    link_approved_tasks,
)


class WeeklySourceLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = normalize_week_scope(
            "user-1",
            "2026-08-10",
            "Asia/Seoul",
            as_of=datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
        )

    def test_links_approved_gmail_and_calendar_sources(self) -> None:
        tasks = [
            TaskRecord(
                task_id="task-calendar",
                assignee_user_id="user-1",
                title="캘린더 후속 조치",
                source_type="calendar",
                source_id="primary:event-42",
            ),
            TaskRecord(
                task_id="task-email",
                assignee_user_id="user-1",
                title="메일 요청 확인",
                source_type="email",
                source_id="gmail-message-7",
            ),
            TaskRecord(task_id="task-manual", assignee_user_id="user-1", title="수동 작업"),
            TaskRecord(
                task_id="task-other-user",
                assignee_user_id="user-2",
                title="다른 사용자 작업",
                source_type="email",
                source_id="gmail-message-other",
            ),
            TaskRecord(
                task_id="task-cancelled",
                assignee_user_id="user-1",
                title="취소된 작업",
                status="cancelled",
                source_type="calendar",
                source_id="primary:event-cancelled",
            ),
        ]

        result = link_approved_tasks(tasks, scope=self.scope)

        self.assertEqual(
            [item.task.task_id for item in result],
            ["task-calendar", "task-email", "task-manual"],
        )
        by_id = {item.task.task_id: item for item in result}
        self.assertEqual(by_id["task-calendar"].source_refs, ("task-calendar", "primary:event-42"))
        self.assertEqual(by_id["task-email"].source_refs, ("task-email", "gmail-message-7"))
        self.assertEqual(by_id["task-manual"].source_refs, ("task-manual",))

    def test_soft_deleted_and_cancelled_tasks_are_excluded(self) -> None:
        deleted_at = datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc)
        tasks = [
            TaskRecord(
                task_id="deleted",
                assignee_user_id="user-1",
                title="삭제된 작업",
                deleted_at=deleted_at,
            ),
            TaskRecord(
                task_id="cancelled",
                assignee_user_id="user-1",
                title="취소된 작업",
                status="cancelled",
                source_type="email",
                source_id="gmail-cancelled",
            ),
        ]

        self.assertEqual(link_approved_tasks(tasks, scope=self.scope), ())

    def test_order_is_deterministic(self) -> None:
        tasks = [
            TaskRecord(task_id="z", assignee_user_id="user-1", title="진행 2"),
            TaskRecord(task_id="a", assignee_user_id="user-1", title="진행 1"),
            TaskRecord(task_id="done", assignee_user_id="user-1", title="완료", status="done"),
        ]

        first = link_approved_tasks(tasks, scope=self.scope)
        second = link_approved_tasks(reversed(tasks), scope=self.scope)
        self.assertEqual(first, second)
        self.assertEqual([item.task.task_id for item in first], ["done", "a", "z"])

    def test_action_item_source_must_exist_in_known_originals(self) -> None:
        tasks = [
            TaskRecord(
                task_id="known-action",
                assignee_user_id="user-1",
                title="회의 후속 조치",
                source_type="action_item",
                source_id="action-1",
            ),
            TaskRecord(
                task_id="unknown-action",
                assignee_user_id="user-1",
                title="원본 없는 조치",
                source_type="action_item",
                source_id="action-missing",
            ),
            TaskRecord(
                task_id="other-user-action",
                assignee_user_id="user-2",
                title="다른 사용자 조치",
                source_type="action_item",
                source_id="action-1",
            ),
        ]

        result = link_action_item_tasks(
            tasks,
            scope=self.scope,
            known_action_item_ids={"action-1"},
        )

        self.assertEqual([item.task.task_id for item in result], ["known-action"])
        self.assertEqual(result[0].source_refs, ("known-action", "action-1"))

    def test_builds_weekly_report_data_from_linked_items(self) -> None:
        tasks = [
            TaskRecord(task_id="done", assignee_user_id="user-1", title="완료", status="done"),
            TaskRecord(
                task_id="mail",
                assignee_user_id="user-1",
                title="메일 확인",
                source_type="email",
                source_id="gmail-1",
            ),
        ]
        items = link_approved_tasks(tasks, scope=self.scope)

        data = build_weekly_report_data(items, scope=self.scope)

        self.assertEqual(data["period"], {"start": "2026-08-10", "end": "2026-08-17"})
        self.assertEqual([item["task_id"] for item in data["completed"]], ["done"])
        self.assertEqual([item["task_id"] for item in data["in_progress"]], ["mail"])
        self.assertEqual(data["delayed"], [])
        self.assertEqual(data["unresolved_issues"], [])
        self.assertEqual(data["next_week_plans"], [])
        self.assertEqual(data["source_refs"], ["done", "mail", "gmail-1"])

    def test_builds_weekly_report_data_routes_blocked_tasks_to_unresolved_issues(self) -> None:
        """`blocked` Task는 `delayed`가 아니라 `unresolved_issues`에 담겨야 한다
        (2026-08-16, 17번 갭 문서 #16)."""

        tasks = [
            TaskRecord(task_id="blocked", assignee_user_id="user-1", title="차단된 작업", status="blocked"),
            TaskRecord(
                task_id="overdue",
                assignee_user_id="user-1",
                title="기한 초과 작업",
                due_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            ),
        ]
        items = link_approved_tasks(tasks, scope=self.scope)

        data = build_weekly_report_data(items, scope=self.scope)

        self.assertEqual([item["task_id"] for item in data["unresolved_issues"]], ["blocked"])
        self.assertEqual([item["task_id"] for item in data["delayed"]], ["overdue"])
        self.assertIn("blocked", data["source_refs"])

    def test_extracts_only_evidence_backed_next_week_plans(self) -> None:
        tasks = [
            TaskRecord(
                task_id="next-week-mail",
                assignee_user_id="user-1",
                title="다음 주 메일 후속",
                source_type="email",
                source_id="gmail-next",
                due_at=datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc),
            ),
            TaskRecord(
                task_id="carry-action",
                assignee_user_id="user-1",
                title="미완료 회의 조치",
                source_type="action_item",
                source_id="action-9",
            ),
            TaskRecord(
                task_id="no-plan",
                assignee_user_id="user-1",
                title="근거 없는 일반 작업",
            ),
            TaskRecord(
                task_id="other-user",
                assignee_user_id="user-2",
                title="다른 사용자 계획",
                source_type="calendar",
                source_id="primary:event-other",
                due_at=datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc),
            ),
        ]

        result = extract_next_week_plans(
            tasks,
            scope=self.scope,
            explicit_plans=(
                ExplicitPlan("명시된 일정", ("calendar:event-1",)),
                ExplicitPlan("근거 없는 문장", ()),
            ),
        )

        self.assertEqual(
            result,
            (
                {"title": "미완료 회의 조치", "kind": "carry_over", "source_refs": ["carry-action", "action-9"]},
                {"title": "다음 주 메일 후속", "kind": "planned", "source_refs": ["next-week-mail", "gmail-next"]},
                {"title": "명시된 일정", "kind": "planned", "source_refs": ["calendar:event-1"]},
            ),
        )


if __name__ == "__main__":
    unittest.main()
