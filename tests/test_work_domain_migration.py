"""M1.1-01 업무 도메인 Migration 계약 테스트."""

from __future__ import annotations

import unittest
from pathlib import Path


class WorkDomainMigrationContractTests(unittest.TestCase):
    """Task·동기화 상태의 업무 저장 책임과 권한 경계를 SQL에서 확인한다."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).parents[1] / "migrations" / "002_work_domain.sql"
        ).read_text(encoding="utf-8")

    def test_declares_only_m11_work_domain_tables(self) -> None:
        for table in ("users", "tasks", "source_sync_states"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", self.sql)
        for a2a_table in ("a2a_tasks", "a2a_messages", "a2a_artifacts", "a2a_checkpoints"):
            self.assertNotIn(f"CREATE TABLE IF NOT EXISTS {a2a_table}", self.sql)

    def test_task_contract_contains_scope_soft_delete_and_source_deduplication(self) -> None:
        for fragment in (
            "assignee_user_id text NOT NULL REFERENCES users(user_id)",
            "deleted_at timestamptz",
            "source_type text NOT NULL CHECK",
            "source_id text",
            "tasks_source_unique_idx",
            "WHERE source_id IS NOT NULL",
            "WHERE deleted_at IS NULL",
        ):
            self.assertIn(fragment, self.sql)

    def test_sync_state_contract_contains_provider_cursors_and_watch_fields(self) -> None:
        for fragment in (
            "sync_user_id text NOT NULL REFERENCES users(user_id)",
            "source_type text NOT NULL CHECK (source_type IN ('gmail', 'google_calendar'))",
            "sync_cursor text",
            "channel_id text",
            "channel_token text",
            "resource_id text",
            "watch_expiration timestamptz",
            "UNIQUE (sync_user_id, source_type)",
        ):
            self.assertIn(fragment, self.sql)

    def test_migration_does_not_embed_secret_or_sample_values(self) -> None:
        lowered = self.sql.lower()
        for forbidden in ("client_secret", "refresh_token", "bearer ", "api_key", "insert into"):
            self.assertNotIn(forbidden, lowered)

    def test_user_id_is_text_not_uuid_because_it_holds_the_raw_oidc_subject(self) -> None:
        """`users.user_id`(및 참조하는 모든 FK 컬럼)는 `uuid`가 아니라 `text`여야 한다 — 실제
        값은 내부에서 발급한 UUID가 아니라 Google OIDC의 `sub` Claim을 그대로 쓰는데, 이 값은
        "104645315427066509691" 같은 큰 10진수 문자열이라 uuid 형식이 아니다. 예전엔 `uuid`로
        선언돼 있어 실제 로그인 사용자로 `search_meetings`를 쓰면
        `psycopg.errors.InvalidTextRepresentation`이 CORS 없는 500으로 새 나가 "Failed to fetch"로
        보였다(2026-08-16, 14번 갭 문서). SQLite 저장소는 처음부터 `user_id TEXT`였다."""

        self.assertIn("user_id text PRIMARY KEY", self.sql)
        self.assertNotIn("user_id uuid", self.sql)

    def test_task_id_is_text_not_uuid_because_approved_proposals_use_deterministic_ids(self) -> None:
        """`tasks.task_id`도 `uuid`가 아니라 `text`여야 한다 — 수동 등록 Task는 `uuid4()`를
        쓰지만, 제안함에서 승인된 Task는 `app/proposal_api.py`가
        `f"proposal-{source_type}-{source_id}"` 형태의 결정적 문자열 ID를 쓴다(같은 제안을
        다시 승인해도 같은 Task가 되도록 하기 위해서). 예전엔 `uuid`로 선언돼 있어 실사용자의
        승인된 Task를 Postgres에 저장하려 하면 `psycopg.errors.InvalidTextRepresentation`이
        났다(2026-08-16, 14번 갭 문서 — `DATABASE_URL` 도입 후 `weekly_report`가 빈 결과만
        반환해 재현). SQLite 스키마는 처음부터 `task_id TEXT`였다."""

        self.assertIn("task_id text PRIMARY KEY", self.sql)
        self.assertNotIn("task_id uuid", self.sql)


if __name__ == "__main__":
    unittest.main()
