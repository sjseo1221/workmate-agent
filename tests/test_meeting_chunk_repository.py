"""M4.1-02 Chunk Repository 계약 테스트."""

from __future__ import annotations

import unittest
from uuid import uuid4

from app.domain.meeting_chunk import MeetingChunkRecord
from app.repositories.meeting_chunks import SQLiteMeetingChunkRepository


def vector(value: float = 0.1) -> tuple[float, ...]:
    """계약 테스트용 1536차원 Vector를 생성한다."""
    return (value,) * 1536


class MeetingChunkRepositoryTests(unittest.TestCase):
    """Chunk 저장·수정과 사용자 격리를 확인한다."""

    def setUp(self) -> None:
        self.repository = SQLiteMeetingChunkRepository()
        self.user_id = "user-a"
        self.other_user_id = "user-b"

    def _chunk(self, *, user_id: str | None = None, sequence_no: int = 0, meeting_id: str = "meeting-1") -> MeetingChunkRecord:
        return MeetingChunkRecord(
            meeting_chunk_id=str(uuid4()), parent_meeting_id=meeting_id, user_id=user_id or self.user_id,
            sequence_no=sequence_no, content="회의에서 일정이 확정되었다.", embedding=vector(), started_at_ms=0, ended_at_ms=1000,
        )

    def test_create_list_and_get_are_scoped_to_user(self) -> None:
        own = self.repository.create(self._chunk())
        other = self.repository.create(self._chunk(user_id=self.other_user_id, meeting_id="meeting-2"))
        self.assertEqual([item.meeting_chunk_id for item in self.repository.list("meeting-1", self.user_id)], [own.meeting_chunk_id])
        self.assertIsNone(self.repository.get(other.meeting_chunk_id, self.user_id))

    def test_same_meeting_sequence_returns_existing_chunk(self) -> None:
        first = self.repository.create(self._chunk())
        duplicate = self.repository.create(self._chunk(sequence_no=0))
        self.assertEqual(duplicate.meeting_chunk_id, first.meeting_chunk_id)

    def test_update_cannot_cross_user_boundary(self) -> None:
        chunk = self.repository.create(self._chunk())
        replacement = MeetingChunkRecord(meeting_chunk_id=chunk.meeting_chunk_id, parent_meeting_id=chunk.parent_meeting_id, user_id=chunk.user_id, sequence_no=chunk.sequence_no, content="변경된 원문", embedding=vector(0.2))
        self.assertIsNone(self.repository.update(replacement, self.other_user_id))
        self.assertEqual(self.repository.get(chunk.meeting_chunk_id, self.user_id).content, chunk.content)

    def test_domain_rejects_wrong_dimension_and_time_range(self) -> None:
        with self.assertRaisesRegex(ValueError, "1536"):
            self._chunk_with_embedding((0.1,))
        with self.assertRaisesRegex(ValueError, "ended_at_ms"):
            MeetingChunkRecord(meeting_chunk_id="c", parent_meeting_id="m", user_id="u", sequence_no=0, content="x", embedding=vector(), started_at_ms=2, ended_at_ms=1)

    def _chunk_with_embedding(self, embedding: tuple[float, ...]) -> MeetingChunkRecord:
        return MeetingChunkRecord(meeting_chunk_id="c", parent_meeting_id="m", user_id="u", sequence_no=0, content="x", embedding=embedding)


if __name__ == "__main__":
    unittest.main()
