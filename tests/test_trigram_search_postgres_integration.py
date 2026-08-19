"""M4.2 pg_trgm 키워드 검색과 metadata·사용자 필터 통합 검수."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from uuid import uuid4

import psycopg

from app.domain.meeting_chunk import MeetingChunkRecord
from app.repositories.meeting_chunks import PostgresMeetingChunkRepository


TEST_DATABASE_URL = os.getenv("WORKMATE_TEST_DATABASE_URL")


def embedding() -> tuple[float, ...]:
    """pgvector 저장에 필요한 1536차원 Fixture를 만든다."""
    return (0.1,) * 1536


@unittest.skipUnless(TEST_DATABASE_URL, "WORKMATE_TEST_DATABASE_URL is not configured")
class TrigramSearchPostgresIntegrationTests(unittest.TestCase):
    """실제 pg_trgm 검색 결과와 사용자·회의 메타데이터 격리를 확인한다."""

    def test_keyword_search_scope_and_metadata_filter(self) -> None:
        repository = PostgresMeetingChunkRepository(TEST_DATABASE_URL)
        owner = str(uuid4())
        other_user = str(uuid4())
        first_meeting = str(uuid4())
        second_meeting = str(uuid4())
        other_meeting = str(uuid4())
        started_at = datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc)
        chunks = [
            MeetingChunkRecord(str(uuid4()), first_meeting, owner, 0, "예산 forecast 결정", embedding()),
            MeetingChunkRecord(str(uuid4()), second_meeting, owner, 0, "제품 디자인 일정", embedding()),
            MeetingChunkRecord(str(uuid4()), other_meeting, other_user, 0, "예산 forecast 외부 사용자", embedding()),
        ]
        try:
            for chunk in chunks:
                repository.create(chunk)
            with psycopg.connect(TEST_DATABASE_URL) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE meetings SET title=%s, started_at=%s WHERE meeting_id=%s AND user_id=%s",
                    ("예산 회의", started_at, first_meeting, owner),
                )
                cursor.execute(
                    "UPDATE meetings SET title=%s, started_at=%s WHERE meeting_id=%s AND user_id=%s",
                    ("제품 회의", datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc), second_meeting, owner),
                )

            hits = repository.search_keyword("예산 forecast", owner)
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].meeting_id, first_meeting)
            self.assertEqual(hits[0].meeting_title, "예산 회의")
            self.assertGreater(hits[0].similarity, 0)

            filtered = repository.search_keyword(
                "예산 forecast", owner, meeting_id=first_meeting,
                started_from=datetime(2026, 8, 1, tzinfo=timezone.utc),
                ended_to=datetime(2026, 8, 15, tzinfo=timezone.utc),
            )
            self.assertEqual([hit.meeting_id for hit in filtered], [first_meeting])
            other_hits = repository.search_keyword("예산 forecast", other_user)
            self.assertEqual(len(other_hits), 1)
            self.assertEqual(other_hits[0].user_id, other_user)
            with self.assertRaises(ValueError):
                repository.search_keyword("   ", owner)
        finally:
            with psycopg.connect(TEST_DATABASE_URL) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM meetings WHERE user_id=%s AND meeting_id IN (%s, %s)",
                    (owner, first_meeting, second_meeting),
                )
                cursor.execute(
                    "DELETE FROM meetings WHERE user_id=%s AND meeting_id=%s",
                    (other_user, other_meeting),
                )
                cursor.execute("DELETE FROM users WHERE user_id=%s", (other_user,))


if __name__ == "__main__":
    unittest.main()
