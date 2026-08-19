"""M4.1-02 PostgreSQL pgvector Chunk Repository 통합 테스트."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from app.domain.meeting_chunk import MeetingChunkRecord
from app.repositories.meeting_chunks import PostgresMeetingChunkRepository


TEST_DATABASE_URL = os.getenv("WORKMATE_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_DATABASE_URL, "WORKMATE_TEST_DATABASE_URL is not configured")
class MeetingChunkPostgresIntegrationTests(unittest.TestCase):
    """실제 pgvector 저장·조회·수정과 사용자 격리를 확인한다."""

    def test_pgvector_repository_scope_idempotency_and_update(self) -> None:
        repository = PostgresMeetingChunkRepository(TEST_DATABASE_URL)
        owner = str(uuid4())
        other = str(uuid4())
        meeting_id = str(uuid4())
        chunk = MeetingChunkRecord(
            meeting_chunk_id=str(uuid4()), parent_meeting_id=meeting_id, user_id=owner,
            sequence_no=0, content="검색 가능한 회의 원문", embedding=(0.1,) * 1536,
            started_at_ms=0, ended_at_ms=1000,
        )

        created = repository.create(chunk)
        duplicate = repository.create(
            MeetingChunkRecord(
                meeting_chunk_id=str(uuid4()), parent_meeting_id=meeting_id, user_id=owner,
                sequence_no=0, content="중복 순서", embedding=(0.2,) * 1536,
            )
        )
        self.assertEqual(duplicate.meeting_chunk_id, created.meeting_chunk_id)
        self.assertEqual(len(created.embedding), 1536)
        self.assertIsNone(repository.get(created.meeting_chunk_id, other))
        self.assertEqual([item.meeting_chunk_id for item in repository.list(meeting_id, owner)], [created.meeting_chunk_id])

        replacement = MeetingChunkRecord(
            meeting_chunk_id=created.meeting_chunk_id, parent_meeting_id=meeting_id, user_id=owner,
            sequence_no=0, content="재임베딩된 회의 원문", embedding=(0.3,) * 1536,
            embedding_version="2",
        )
        self.assertIsNone(repository.update(replacement, other))
        updated = repository.update(replacement, owner)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.content, replacement.content)
        self.assertEqual(updated.embedding_version, "2")
        self.assertEqual(len(updated.embedding), 1536)

    def test_create_writes_real_meeting_title_and_started_at_and_self_heals_a_placeholder_row(self) -> None:
        """예전엔 `create()`가 부모 `meetings` 행을 `title="회의"`·`started_at=NULL` 고정값으로
        만들어서, `search_meetings`가 인용할 실제 회의 날짜가 없어 `ValueError`를 냈다(2026-08-16,
        14번 갭 문서 — 실사용자 `search_meetings` 첫 실행에서 재현). 이제 `chunk.meeting_*`를
        `meetings` 행에 실제로 채우고, 두 번째 `create()`가 `ON CONFLICT DO UPDATE`로 이전에
        잘못 저장된 자리표시자 값도 자동으로 고쳐 쓰는지(자체 치유) 확인한다."""

        repository = PostgresMeetingChunkRepository(TEST_DATABASE_URL)
        owner = str(uuid4())
        meeting_id = str(uuid4())
        started_at = datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc)

        # 첫 Chunk는 메타데이터 없이(예전 버그를 흉내) 들어와 placeholder로 저장된다.
        repository.create(
            MeetingChunkRecord(
                meeting_chunk_id=str(uuid4()), parent_meeting_id=meeting_id, user_id=owner,
                sequence_no=0, content="첫 발화", embedding=(0.1,) * 1536,
            )
        )
        # 두 번째 Chunk가 실제 회의 메타데이터를 갖고 들어오면 같은 `meetings` 행을 고쳐 쓴다.
        repository.create(
            MeetingChunkRecord(
                meeting_chunk_id=str(uuid4()), parent_meeting_id=meeting_id, user_id=owner,
                sequence_no=1, content="두 번째 발화", embedding=(0.2,) * 1536,
                meeting_title="주간 스탠드업", meeting_started_at=started_at,
            )
        )

        with psycopg.connect(TEST_DATABASE_URL, row_factory=dict_row) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT title, started_at FROM meetings WHERE meeting_id = %s", (meeting_id,))
            row = cursor.fetchone()
        self.assertEqual(row["title"], "주간 스탠드업")
        self.assertEqual(row["started_at"], started_at)


if __name__ == "__main__":
    unittest.main()
