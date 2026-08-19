"""제안함 "무시" 기록 Repository 계약 테스트(17번 갭 문서 #2, 2026-08-16)."""

from __future__ import annotations

import unittest

from app.repositories.ignored_proposals import SQLiteIgnoredProposalRepository


class IgnoredProposalRepositoryTests(unittest.TestCase):
    """무시 기록이 사용자·출처 유형별로 격리되고 멱등하게 쌓이는지 검증한다."""

    def setUp(self) -> None:
        self.repository = SQLiteIgnoredProposalRepository()

    def test_marked_source_id_shows_up_in_ignored_source_ids(self) -> None:
        self.repository.mark("user-a", "email", "m-1")
        self.assertEqual(self.repository.ignored_source_ids("user-a", "email"), {"m-1"})

    def test_marking_the_same_source_twice_is_idempotent(self) -> None:
        self.repository.mark("user-a", "email", "m-1")
        self.repository.mark("user-a", "email", "m-1")
        self.assertEqual(self.repository.ignored_source_ids("user-a", "email"), {"m-1"})

    def test_isolated_by_user(self) -> None:
        self.repository.mark("user-a", "email", "m-1")
        self.assertEqual(self.repository.ignored_source_ids("user-b", "email"), set())

    def test_isolated_by_source_type(self) -> None:
        self.repository.mark("user-a", "email", "shared-id")
        self.assertEqual(self.repository.ignored_source_ids("user-a", "calendar"), set())

    def test_multiple_candidates_from_the_same_message_accumulate(self) -> None:
        """Gmail 한 메일에서 나온 여러 후보(`message_id:index`)를 각각
        무시해도 서로 다른 기록으로 쌓인다 — 호출자가 접두어로 "메일
        전체가 무시됐는지"를 판단할 수 있게 하기 위해서다."""

        self.repository.mark("user-a", "email", "m-1:0")
        self.repository.mark("user-a", "email", "m-1:1")
        self.assertEqual(self.repository.ignored_source_ids("user-a", "email"), {"m-1:0", "m-1:1"})


if __name__ == "__main__":
    unittest.main()
