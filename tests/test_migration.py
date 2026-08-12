"""M0.1-03 A2A 인프라 Migration 계약 테스트."""

from __future__ import annotations

import unittest
from pathlib import Path


class MigrationContractTests(unittest.TestCase):
    """업무 도메인과 분리된 A2A 저장 책임이 SQL에 선언됐는지 확인한다."""

    def test_a2a_infrastructure_migration_declares_required_tables(self) -> None:
        migration = Path(__file__).parents[1] / "migrations" / "001_a2a_infrastructure.sql"
        sql = migration.read_text(encoding="utf-8")
        for table in (
            "a2a_tasks",
            "a2a_messages",
            "a2a_artifacts",
            "a2a_checkpoints",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", sql)
        self.assertIn("thread_id text NOT NULL UNIQUE", sql)
        self.assertIn("request_hash text NOT NULL", sql)
        self.assertIn("expires_at timestamptz", sql)


if __name__ == "__main__":
    unittest.main()
