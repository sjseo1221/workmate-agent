"""M4.4 실제 Hybrid 검색 결과의 답변·출처 통합 검수."""

from __future__ import annotations

import os
import unittest
from uuid import uuid4

from app.domain.meeting_chunk import MeetingChunkRecord
from app.meeting_answer import MeetingAnswerService
from app.repositories.meeting_chunks import PostgresMeetingChunkRepository


TEST_DATABASE_URL = os.getenv("WORKMATE_TEST_DATABASE_URL")


def vector() -> tuple[float, ...]:
    """답변 통합 검수용 1536차원 Vector를 생성한다."""
    return (1.0,) + (0.0,) * 1535


@unittest.skipUnless(TEST_DATABASE_URL, "WORKMATE_TEST_DATABASE_URL is not configured")
class MeetingAnswerPostgresIntegrationTests(unittest.TestCase):
    """실제 PostgreSQL Hybrid 결과가 출처 답변으로 연결되는지 확인한다."""

    def test_answer_contains_meeting_and_chunk_source(self) -> None:
        repository = PostgresMeetingChunkRepository(TEST_DATABASE_URL)
        user_id = str(uuid4())
        meeting_id = str(uuid4())
        chunk = MeetingChunkRecord(
            str(uuid4()), meeting_id, user_id, 0,
            "예산 승인 금액은 100만원으로 결정", vector(),
        )
        try:
            repository.create(chunk)
            import psycopg
            with psycopg.connect(TEST_DATABASE_URL) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE meetings SET title=%s WHERE user_id=%s AND meeting_id=%s",
                    ("예산 회의", user_id, meeting_id),
                )
            hits = repository.search_hybrid("예산 승인", vector(), user_id, limit=5)
            result = MeetingAnswerService().answer_from_hits("예산 승인", hits)
            self.assertTrue(result.grounded)
            self.assertIn("예산 승인 금액은 100만원으로 결정", result.answer)
            self.assertIn("예산 회의", result.answer)
            self.assertIn(chunk.meeting_chunk_id, result.sources[0].source_id)
        finally:
            with psycopg.connect(TEST_DATABASE_URL) as connection, connection.cursor() as cursor:
                cursor.execute("DELETE FROM meetings WHERE user_id=%s AND meeting_id=%s", (user_id, meeting_id))


if __name__ == "__main__":
    unittest.main()
