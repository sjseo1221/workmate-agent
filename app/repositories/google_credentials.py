"""사용자별 Google OAuth Credential 저장소.

"제안함" 화면의 Gmail·Calendar 동기화는 지금까지 `google-oauth-test/token.json`
파일 하나(고정 데모 계정)만 모든 사용자가 공유했다(15번 문서 "📬 제안함" 절,
2026-08-18 "다른 담당자가 실행할 때..." 논의 참고). 이 저장소는 사용자가 직접
`app/google_oauth_web.py`의 웹 OAuth 흐름으로 자기 Google 계정을 연결하면 그
Credential을 `user_id` 별로 저장한다 — 기존 공유 파일 방식은 그대로 남겨두고
(원칙: 기존 기능 비영향), 사용자별 연결이 있으면 그걸 우선 쓰도록 호출부에서
폴백 순서를 정한다.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from functools import lru_cache
import os
from pathlib import Path
import sqlite3
from typing import Protocol
from uuid import uuid4

try:
    import psycopg
except ImportError:  # pragma: no cover - SQLite-only 환경
    psycopg = None  # type: ignore[assignment]


DATABASE_URL_ENV = "DATABASE_URL"
GOOGLE_CREDENTIAL_DB_PATH_ENV = "WORKMATE_GOOGLE_CREDENTIAL_DB_PATH"


class GoogleCredentialRepository(Protocol):
    """사용자별 Google Credential 저장 계약."""

    def save(self, user_id: str, token_json: str) -> None: ...

    def load(self, user_id: str) -> str | None: ...

    def delete(self, user_id: str) -> None: ...


class SQLiteGoogleCredentialRepository:
    """단위 테스트와 로컬 개발용 Adapter."""

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        self.database_path = str(database_path)
        self._use_uri = self.database_path == ":memory:"
        self._connection_target = (
            f"file:workmate-google-credentials-{uuid4().hex}?mode=memory&cache=shared"
            if self._use_uri
            else self.database_path
        )
        self._anchor = sqlite3.connect(self._connection_target, uri=True) if self._use_uri else None
        if not self._use_uri:
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._connection_target, uri=self._use_uri)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS google_credentials (
                    user_id TEXT PRIMARY KEY,
                    token_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def save(self, user_id: str, token_json: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO google_credentials(user_id, token_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET token_json=excluded.token_json, updated_at=excluded.updated_at
                """,
                (user_id, token_json, datetime.now(timezone.utc).isoformat()),
            )

    def load(self, user_id: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT token_json FROM google_credentials WHERE user_id = ?", (user_id,)
            ).fetchone()
        return str(row["token_json"]) if row else None

    def delete(self, user_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM google_credentials WHERE user_id = ?", (user_id,))


class PostgresGoogleCredentialRepository:
    """운영 PostgreSQL용 Adapter."""

    def __init__(self, dsn: str) -> None:
        if psycopg is None:
            raise RuntimeError("psycopg is required for PostgresGoogleCredentialRepository")
        self.dsn = dsn
        self._initialized = False

    def _connect(self):
        return psycopg.connect(self.dsn)

    def _initialize(self) -> None:
        if self._initialized:
            return
        migration = (Path(__file__).resolve().parents[2] / "migrations" / "006_google_credentials.sql").read_text(encoding="utf-8")
        with self._connect() as connection, connection.cursor() as cursor:
            for statement in migration.split(";"):
                if statement.strip():
                    cursor.execute(statement)
        self._initialized = True

    def _ensure_user(self, connection, user_id: str) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO users(user_id, display_name, timezone) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING",
                (user_id, user_id, "Asia/Seoul"),
            )

    def save(self, user_id: str, token_json: str) -> None:
        self._initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            self._ensure_user(connection, user_id)
            cursor.execute(
                """
                INSERT INTO google_credentials(user_id, token_json, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (user_id) DO UPDATE SET token_json=excluded.token_json, updated_at=excluded.updated_at
                """,
                (user_id, token_json),
            )

    def load(self, user_id: str) -> str | None:
        self._initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT token_json FROM google_credentials WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
        return str(row[0]) if row else None

    def delete(self, user_id: str) -> None:
        self._initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM google_credentials WHERE user_id = %s", (user_id,))


@lru_cache(maxsize=1)
def google_credential_repository() -> GoogleCredentialRepository:
    """환경 설정에 맞는 단일 Google Credential Repository를 반환한다."""

    database_url = os.getenv(DATABASE_URL_ENV)
    if database_url:
        return PostgresGoogleCredentialRepository(database_url)
    return SQLiteGoogleCredentialRepository(os.getenv(GOOGLE_CREDENTIAL_DB_PATH_ENV, ".runtime/google_credentials.sqlite3"))


__all__ = [
    "GoogleCredentialRepository",
    "SQLiteGoogleCredentialRepository",
    "PostgresGoogleCredentialRepository",
    "google_credential_repository",
    "DATABASE_URL_ENV",
    "GOOGLE_CREDENTIAL_DB_PATH_ENV",
]
