"""회의·Action Item 저장소의 재조회·수정·거절 계약 테스트(2026-08-16, 17번 갭 문서 #8·#10)."""

from __future__ import annotations

import unittest

from app.domain.meeting import MeetingRecord
from app.repositories.meetings import SQLiteMeetingRepository


class MeetingRepositoryActionAndSummaryTests(unittest.TestCase):
    """Action Item 수정·거절과 회의 요약 재조회가 사용자·회의 범위로 격리되는지 검증한다."""

    def setUp(self) -> None:
        self.repo = SQLiteMeetingRepository()
        self.repo.create(MeetingRecord("m-1", "user-a", "테스트 회의"))

    def test_set_summary_persists_and_round_trips(self) -> None:
        self.repo.set_summary("m-1", "user-a", "요약 문장입니다.")
        record = self.repo.get("m-1", "user-a")
        assert record is not None
        self.assertEqual(record.summary, "요약 문장입니다.")

    def test_new_meeting_has_no_summary_until_set(self) -> None:
        record = self.repo.get("m-1", "user-a")
        assert record is not None
        self.assertIsNone(record.summary)

    def test_upsert_action_stores_extended_fields(self) -> None:
        self.repo.upsert_action(
            "a-1", "m-1", "user-a", "QA 결과 공유", "오늘 오후까지 결과를 공유",
            due_at="2026-08-20T00:00:00+00:00", assignee_user_id="user-a",
            start_ms=1000, end_ms=2000, meeting_chunk_id="chunk-1",
        )
        [action] = self.repo.list_actions("m-1", "user-a")
        self.assertEqual(action["due_at"], "2026-08-20T00:00:00+00:00")
        self.assertEqual(action["assignee_user_id"], "user-a")
        self.assertEqual(action["start_ms"], 1000)
        self.assertEqual(action["end_ms"], 2000)
        self.assertEqual(action["meeting_chunk_id"], "chunk-1")

    def test_list_actions_is_scoped_to_meeting_and_user(self) -> None:
        self.repo.create(MeetingRecord("m-2", "user-a", "다른 회의"))
        self.repo.upsert_action("a-1", "m-1", "user-a", "제목1", "근거1")
        self.repo.upsert_action("a-2", "m-2", "user-a", "제목2", "근거2")
        self.repo.upsert_action("a-3", "m-1", "user-b", "제목3", "근거3")
        actions = self.repo.list_actions("m-1", "user-a")
        self.assertEqual([a["action_item_id"] for a in actions], ["a-1"])

    def test_edit_action_updates_only_provided_fields_and_leaves_evidence_text_untouched(self) -> None:
        self.repo.upsert_action("a-1", "m-1", "user-a", "원래 제목", "원본 근거 문장")
        edited = self.repo.edit_action("a-1", "m-1", "user-a", due_at="2026-08-21T00:00:00+00:00")
        assert edited is not None
        self.assertEqual(edited["title"], "원래 제목")  # 안 넘긴 필드는 그대로
        self.assertEqual(edited["due_at"], "2026-08-21T00:00:00+00:00")
        self.assertEqual(edited["evidence_text"], "원본 근거 문장")  # 읽기 전용, 절대 안 바뀜

    def test_edit_action_can_change_title(self) -> None:
        self.repo.upsert_action("a-1", "m-1", "user-a", "원래 제목", "근거")
        edited = self.repo.edit_action("a-1", "m-1", "user-a", title="고친 제목")
        assert edited is not None
        self.assertEqual(edited["title"], "고친 제목")

    def test_edit_action_returns_none_for_missing_action(self) -> None:
        self.assertIsNone(self.repo.edit_action("missing", "m-1", "user-a", title="x"))

    def test_reject_action_marks_rejected_without_creating_task(self) -> None:
        self.repo.upsert_action("a-1", "m-1", "user-a", "제목", "근거")
        rejected = self.repo.reject_action("a-1", "m-1", "user-a")
        assert rejected is not None
        self.assertEqual(rejected["approval_status"], "rejected")
        self.assertIsNone(rejected["task_id"])

    def test_set_title_persists_and_round_trips(self) -> None:
        self.repo.set_title("m-1", "user-a", "새 제목")
        record = self.repo.get("m-1", "user-a")
        assert record is not None
        self.assertEqual(record.title, "새 제목")

    def test_delete_removes_the_meeting_from_get_and_list(self) -> None:
        """회의 삭제 기능 추가(2026-08-17, 사용자 요청) — Soft Delete라 `deleted_at`만
        찍히지만, 조회·목록에서는 없는 것처럼 보여야 한다."""

        self.assertTrue(self.repo.delete("m-1", "user-a"))
        self.assertIsNone(self.repo.get("m-1", "user-a"))
        self.assertEqual(self.repo.list("user-a"), [])

    def test_delete_is_scoped_to_the_owning_user(self) -> None:
        self.assertFalse(self.repo.delete("m-1", "user-b"))
        record = self.repo.get("m-1", "user-a")
        self.assertIsNotNone(record)

    def test_delete_returns_false_for_an_unknown_meeting(self) -> None:
        self.assertFalse(self.repo.delete("missing", "user-a"))

    def test_delete_is_idempotent(self) -> None:
        self.assertTrue(self.repo.delete("m-1", "user-a"))
        self.assertFalse(self.repo.delete("m-1", "user-a"))

    def test_get_with_include_deleted_still_finds_a_deleted_meeting(self) -> None:
        """사용자 요청(2026-08-17)의 회귀 테스트 — 회의를 삭제해도 그 회의에서 만든
        Task의 "회의록에서 보기" 근거 링크는 계속 연결돼야 한다."""

        self.repo.delete("m-1", "user-a")
        self.assertIsNone(self.repo.get("m-1", "user-a"))
        found = self.repo.get("m-1", "user-a", include_deleted=True)
        self.assertIsNotNone(found)
        self.assertEqual(found.meeting_id, "m-1")

    def test_reject_action_does_not_override_an_already_approved_item(self) -> None:
        """이미 승인돼 Task까지 만든 Action Item은 거절로 되돌릴 수 없다 —
        `approve_action`이 `task_id`를 한 번만 기록하는 것과 같은 멱등 규칙."""

        self.repo.upsert_action("a-1", "m-1", "user-a", "제목", "근거")
        self.repo.approve_action("a-1", "m-1", "user-a", "task-1")
        result = self.repo.reject_action("a-1", "m-1", "user-a")
        assert result is not None
        self.assertEqual(result["approval_status"], "approved")
        self.assertEqual(result["task_id"], "task-1")


if __name__ == "__main__":
    unittest.main()
