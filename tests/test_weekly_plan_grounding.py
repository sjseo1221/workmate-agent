"""M2.2-02 주간 계획 제안·확정 근거 경계 검증."""

from __future__ import annotations

import unittest

from app.workflows.weekly_report import validate_next_week_plans


class WeeklyPlanGroundingTests(unittest.TestCase):
    def test_accepts_only_grounded_plan_kinds(self) -> None:
        result = validate_next_week_plans(
            [
                {"title": "출시 준비", "kind": "planned", "source_refs": ["task-1"]},
                {"title": "이월 조치", "kind": "carry_over", "source_refs": ["action-1"]},
                {"title": "제안 일정", "kind": "suggestion", "source_refs": ["calendar-1"]},
            ],
            allowed_source_refs={"task-1", "action-1", "calendar-1"},
        )

        self.assertEqual(len(result.accepted), 3)
        self.assertEqual(result.rejected, ())

    def test_rejects_ungrounded_and_unknown_source_plans(self) -> None:
        result = validate_next_week_plans(
            [
                {"title": "근거 없는 확정", "kind": "planned", "source_refs": []},
                {"title": "허용되지 않은 원본", "kind": "planned", "source_refs": ["unknown"]},
                {"title": "근거 없는 제안", "kind": "suggestion"},
            ],
            allowed_source_refs={"task-1"},
        )

        self.assertEqual(result.accepted, ())
        self.assertEqual([item["reason"] for item in result.rejected], ["evidence_required"] * 3)

    def test_normalizes_title_and_deduplicates_source_refs(self) -> None:
        result = validate_next_week_plans(
            [{"title": "  문서 검토  ", "kind": "planned", "source_refs": ["task-1", "task-1", "unknown"]}],
            allowed_source_refs={"task-1"},
        )

        self.assertEqual(
            result.accepted,
            ({"title": "문서 검토", "kind": "planned", "source_refs": ["task-1"]},),
        )


if __name__ == "__main__":
    unittest.main()
