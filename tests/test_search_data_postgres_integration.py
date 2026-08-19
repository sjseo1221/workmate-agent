"""M4.1-04 PostgreSQL·pgvector·재임베딩·HNSW 통합 검수."""

from __future__ import annotations

import os
import unittest
from uuid import uuid4

import psycopg

from app.domain.meeting_chunk import MeetingChunkRecord
from app.embeddings import EMBEDDING_DIMENSION, ReembeddingService
from app.repositories.meeting_chunks import PostgresMeetingChunkRepository


TEST_DATABASE_URL = os.getenv("WORKMATE_TEST_DATABASE_URL")


def vector(first: float) -> tuple[float, ...]:
    """pgvector 통합 검수용 1536차원 Vector를 만든다."""
    return (first,) + (0.0,) * (EMBEDDING_DIMENSION - 1)


class FixtureProvider:
    """외부 호출 없이 재임베딩 저장 경계를 검증하는 Fixture Provider."""

    def embed(self, texts: list[str]) -> list[tuple[float, ...]]:
        return [vector(0.8) for _ in texts]


@unittest.skipUnless(TEST_DATABASE_URL, "WORKMATE_TEST_DATABASE_URL is not configured")
class SearchDataPostgresIntegrationTests(unittest.TestCase):
    """실제 PostgreSQL에서 재임베딩과 HNSW Index Scan을 확인한다."""

    def test_reembedding_and_hnsw_index_scan(self) -> None:
        repository = PostgresMeetingChunkRepository(TEST_DATABASE_URL)
        owner = str(uuid4())
        meeting_id = str(uuid4())
        chunks = [
            MeetingChunkRecord(
                meeting_chunk_id=str(uuid4()),
                parent_meeting_id=meeting_id,
                user_id=owner,
                sequence_no=index,
                content=f"검색 Chunk {index}",
                embedding=vector(0.01 + index / 1000),
            )
            for index in range(200)
        ]
        try:
            for chunk in chunks:
                repository.create(chunk)

            result = ReembeddingService(FixtureProvider(), repository).reembed(
                chunks[:3], owner
            )
            self.assertEqual(result.updated_count, 3)
            self.assertEqual(repository.get(chunks[0].meeting_chunk_id, owner).embedding_version, "2")
            self.assertEqual(len(repository.get(chunks[0].meeting_chunk_id, owner).embedding), 1536)

            vector_literal = PostgresMeetingChunkRepository._vector_literal(vector(0.8))
            with psycopg.connect(TEST_DATABASE_URL) as connection, connection.cursor() as cursor:
                cursor.execute("SET enable_seqscan = off")
                cursor.execute("SET enable_bitmapscan = off")
                cursor.execute(
                    "EXPLAIN (FORMAT TEXT) SELECT meeting_chunk_id FROM meeting_chunks "
                    "ORDER BY embedding <=> %s::vector LIMIT 5",
                    (vector_literal,),
                )
                plan = "\n".join(str(row[0]) for row in cursor.fetchall())
            self.assertIn("meeting_chunks_embedding_idx", plan)
            self.assertEqual(len(repository.list(meeting_id, owner)), 200)
        finally:
            with psycopg.connect(TEST_DATABASE_URL) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM meetings WHERE meeting_id = %s AND user_id = %s",
                    (meeting_id, owner),
                )


if __name__ == "__main__":
    unittest.main()
