"""M4.3 Dense·Trigram RRF와 HNSW 통합 검수."""

from __future__ import annotations

import os
import unittest
from uuid import uuid4

import psycopg

from app.domain.meeting_chunk import MeetingChunkRecord
from app.repositories.meeting_chunks import PostgresMeetingChunkRepository


TEST_DATABASE_URL = os.getenv("WORKMATE_TEST_DATABASE_URL")


def vector(first: float, second: float = 0.0) -> tuple[float, ...]:
    """검색 Fixture용 1536차원 Vector를 생성한다."""
    return (first, second) + (0.0,) * 1534


@unittest.skipUnless(TEST_DATABASE_URL, "WORKMATE_TEST_DATABASE_URL is not configured")
class HybridSearchPostgresIntegrationTests(unittest.TestCase):
    """실제 PostgreSQL에서 Dense·Trigram RRF와 HNSW 계획을 확인한다."""

    def test_hybrid_rrf_scope_and_hnsw_plan(self) -> None:
        repository = PostgresMeetingChunkRepository(TEST_DATABASE_URL)
        owner = str(uuid4())
        other_user = str(uuid4())
        meeting_id = str(uuid4())
        other_meeting_id = str(uuid4())
        chunks = [
            MeetingChunkRecord(str(uuid4()), meeting_id, owner, 0, "예산 forecast 결정", vector(0.5, 0.5)),
            MeetingChunkRecord(str(uuid4()), meeting_id, owner, 1, "제품 일정 논의", vector(1.0)),
            MeetingChunkRecord(str(uuid4()), other_meeting_id, other_user, 0, "예산 forecast 외부", vector(0.5, 0.5)),
        ]
        chunks.extend(
            MeetingChunkRecord(str(uuid4()), meeting_id, owner, index, f"일반 회의 내용 {index}", vector(0.0, 1.0))
            for index in range(2, 122)
        )
        try:
            for chunk in chunks:
                repository.create(chunk)

            dense = repository.search_dense(vector(1.0), owner, limit=5)
            self.assertEqual(dense[0].content, "제품 일정 논의")
            hybrid = repository.search_hybrid("예산 forecast", vector(1.0), owner, limit=5)
            self.assertEqual({hit.user_id for hit in hybrid}, {owner})
            self.assertTrue(hybrid)
            self.assertTrue(all(hit.rrf_score > 0 for hit in hybrid))
            self.assertTrue(any(hit.dense_rank and hit.keyword_rank for hit in hybrid))
            self.assertEqual(repository.search_hybrid("예산 forecast", vector(1.0), other_user, limit=5)[0].user_id, other_user)

            vector_literal = PostgresMeetingChunkRepository._vector_literal(vector(1.0))
            with psycopg.connect(TEST_DATABASE_URL) as connection, connection.cursor() as cursor:
                cursor.execute("SET enable_seqscan = off")
                cursor.execute(
                    "EXPLAIN (FORMAT TEXT) SELECT meeting_chunk_id FROM meeting_chunks "
                    "ORDER BY embedding <=> %s::vector LIMIT 5",
                    (vector_literal,),
                )
                plan = "\n".join(str(row[0]) for row in cursor.fetchall())
            self.assertIn("meeting_chunks_embedding_idx", plan)
        finally:
            with psycopg.connect(TEST_DATABASE_URL) as connection, connection.cursor() as cursor:
                cursor.execute("DELETE FROM meetings WHERE user_id=%s AND meeting_id IN (%s, %s)", (owner, meeting_id, other_meeting_id))
                cursor.execute("DELETE FROM meetings WHERE user_id=%s AND meeting_id=%s", (other_user, other_meeting_id))


if __name__ == "__main__":
    unittest.main()
