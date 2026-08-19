"""M4.1-01 검색 데이터 Migration 계약 테스트."""

from __future__ import annotations

from pathlib import Path
import unittest


class SearchDataMigrationContractTests(unittest.TestCase):
    """검색 Chunk의 차원·사용자 범위·시간 불변식을 확인한다."""

    def test_search_data_migration_declares_vector_and_scope_contract(self) -> None:
        migration = Path(__file__).parents[1] / "migrations" / "004_search_data.sql"
        sql = migration.read_text(encoding="utf-8")

        for statement in (
            "CREATE EXTENSION IF NOT EXISTS vector",
            "CREATE EXTENSION IF NOT EXISTS pg_trgm",
            "CREATE TABLE IF NOT EXISTS meetings",
            "CREATE TABLE IF NOT EXISTS meeting_chunks",
            "embedding vector(1536) NOT NULL",
            "embedding_model text NOT NULL DEFAULT 'openai/text-embedding-3-small'",
            "FOREIGN KEY (parent_meeting_id, user_id)",
            "UNIQUE (parent_meeting_id, user_id, sequence_no)",
            "ended_at_ms >= started_at_ms",
            "vector_cosine_ops",
            "gin_trgm_ops",
        ):
            self.assertIn(statement, sql)


if __name__ == "__main__":
    unittest.main()
