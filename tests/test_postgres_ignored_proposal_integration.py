"""제안함 "무시" 기록 PostgreSQL 실제 연결 검증(17번 갭 문서 #2, 2026-08-16)."""

from __future__ import annotations

import os
import unittest
from uuid import uuid4

from app.repositories.ignored_proposals import PostgresIgnoredProposalRepository

TEST_DATABASE_URL = os.getenv("WORKMATE_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_DATABASE_URL, "WORKMATE_TEST_DATABASE_URL is not configured")
class PostgresIgnoredProposalIntegrationTests(unittest.TestCase):
    """PostgreSQL에서 무시 기록의 사용자 격리와 멱등 삽입을 확인한다.

    `user_id`는 uuid가 아니라 text 컬럼이어야 한다 — Google OIDC의 `sub`
    Claim(큰 10진수 문자열)을 그대로 쓴다(2026-08-16, 다섯·여섯 번째 버그와
    같은 원인 재발 방지). 그래서 여기서는 일부러 실제 OIDC subject 모양의
    값을 쓴다.
    """

    def test_mark_is_idempotent_and_scoped_by_user_and_source_type(self) -> None:
        repository = PostgresIgnoredProposalRepository(TEST_DATABASE_URL)
        user_id = "104645315427066509691"  # 실제 Google OIDC sub과 같은 모양(uuid 아님)
        other_user_id = str(uuid4())
        message_id = f"m-{uuid4().hex}"

        repository.mark(user_id, "email", message_id)
        repository.mark(user_id, "email", message_id)  # 두 번째 호출은 조용히 무시(멱등)

        self.assertIn(message_id, repository.ignored_source_ids(user_id, "email"))
        self.assertNotIn(message_id, repository.ignored_source_ids(other_user_id, "email"))
        self.assertNotIn(message_id, repository.ignored_source_ids(user_id, "calendar"))


if __name__ == "__main__":
    unittest.main()
