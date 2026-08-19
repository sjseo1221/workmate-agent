"""사용자별 Google Credential 저장소(`app/repositories/google_credentials.py`) 계약 테스트."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.repositories.google_credentials import SQLiteGoogleCredentialRepository


class SQLiteGoogleCredentialRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repository = SQLiteGoogleCredentialRepository(Path(self.temp_dir.name) / "google_credentials.sqlite3")

    def test_missing_user_returns_none(self) -> None:
        self.assertIsNone(self.repository.load("user-a"))

    def test_save_then_load_round_trips(self) -> None:
        self.repository.save("user-a", '{"token": "t1"}')
        self.assertEqual(self.repository.load("user-a"), '{"token": "t1"}')

    def test_save_overwrites_existing_credential_for_the_same_user(self) -> None:
        self.repository.save("user-a", '{"token": "t1"}')
        self.repository.save("user-a", '{"token": "t2"}')
        self.assertEqual(self.repository.load("user-a"), '{"token": "t2"}')

    def test_users_are_isolated(self) -> None:
        self.repository.save("user-a", '{"token": "a"}')
        self.repository.save("user-b", '{"token": "b"}')
        self.assertEqual(self.repository.load("user-a"), '{"token": "a"}')
        self.assertEqual(self.repository.load("user-b"), '{"token": "b"}')

    def test_delete_removes_the_credential(self) -> None:
        self.repository.save("user-a", '{"token": "t1"}')
        self.repository.delete("user-a")
        self.assertIsNone(self.repository.load("user-a"))

    def test_delete_on_missing_user_does_not_raise(self) -> None:
        self.repository.delete("never-existed")  # 예외 없이 조용히 통과해야 한다


if __name__ == "__main__":
    unittest.main()
