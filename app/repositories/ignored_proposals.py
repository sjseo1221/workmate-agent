"""제안함에서 "무시"한 Gmail·Calendar 후보를 영구 제외하기 위한 최소 저장소.

"제안은 DB에 저장하지 않는다"(04 §7) 원칙은 제안 본문·제목·LLM 근거를 저장하지
않는다는 뜻이다 — 이 저장소는 그런 내용을 전혀 담지 않고, 사용자가 이미
결정했다는 사실(ID·시각)만 남긴다. 승인(approve)은 이미 Task 테이블에
`source_type`/`source_id`로 남아 있어 별도 저장이 필요 없었지만, 무시(ignore)는
Task를 만들지 않아 대응하는 기록이 아예 없었다 — 그래서 재동기화 때마다
같은 후보가 다시 노출됐다(17번 갭 문서 #2, 2026-08-16 실사용 중 발견 및 결정).
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Literal, Protocol
from uuid import uuid4

ProposalSourceType = Literal["email", "calendar"]

try:
    import psycopg
except ImportError:  # pragma: no cover - SQLite-only 환경
    psycopg = None  # type: ignore[assignment]


class IgnoredProposalRepository(Protocol):
    """사용자·출처별 "무시" 기록 저장 계약."""

    def mark(self, user_id: str, source_type: ProposalSourceType, source_id: str) -> None: ...

    def ignored_source_ids(self, user_id: str, source_type: ProposalSourceType) -> set[str]: ...


class SQLiteIgnoredProposalRepository:
    """단위·계약 테스트와 로컬 개발용 Adapter."""

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        self.database_path = str(database_path)
        self._use_uri = self.database_path == ":memory:"
        self._connection_target = (
            f"file:workmate-ignored-proposals-{uuid4().hex}?mode=memory&cache=shared"
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
                CREATE TABLE IF NOT EXISTS ignored_proposals (
                    user_id TEXT NOT NULL,
                    source_type TEXT NOT NULL CHECK (source_type IN ('email', 'calendar')),
                    source_id TEXT NOT NULL,
                    ignored_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, source_type, source_id)
                );
                """
            )

    def mark(self, user_id: str, source_type: ProposalSourceType, source_id: str) -> None:
        """이 후보를 무시함으로 기록한다(이미 있으면 그대로 둔다 — 멱등)."""

        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR IGNORE INTO ignored_proposals(user_id, source_type, source_id, ignored_at) VALUES (?, ?, ?, ?)",
                (user_id, source_type, source_id, datetime.now(timezone.utc).isoformat()),
            )

    def ignored_source_ids(self, user_id: str, source_type: ProposalSourceType) -> set[str]:
        """이 사용자·출처에서 무시된 모든 `source_id`를 반환한다."""

        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT source_id FROM ignored_proposals WHERE user_id = ? AND source_type = ?",
                (user_id, source_type),
            ).fetchall()
        return {str(row["source_id"]) for row in rows}


class PostgresIgnoredProposalRepository:
    """운영 PostgreSQL용 Adapter."""

    def __init__(self, dsn: str) -> None:
        if psycopg is None:
            raise RuntimeError("psycopg is required for PostgresIgnoredProposalRepository")
        self.dsn = dsn
        self._initialized = False

    def _connect(self):
        return psycopg.connect(self.dsn)

    def _initialize(self) -> None:
        if self._initialized:
            return
        migration = (Path(__file__).resolve().parents[2] / "migrations" / "005_ignored_proposals.sql").read_text(encoding="utf-8")
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

    def mark(self, user_id: str, source_type: ProposalSourceType, source_id: str) -> None:
        """이 후보를 무시함으로 기록한다(이미 있으면 그대로 둔다 — 멱등)."""

        self._initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            self._ensure_user(connection, user_id)
            cursor.execute(
                """
                INSERT INTO ignored_proposals(user_id, source_type, source_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, source_type, source_id) DO NOTHING
                """,
                (user_id, source_type, source_id),
            )

    def ignored_source_ids(self, user_id: str, source_type: ProposalSourceType) -> set[str]:
        """이 사용자·출처에서 무시된 모든 `source_id`를 반환한다."""

        self._initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT source_id FROM ignored_proposals WHERE user_id = %s AND source_type = %s",
                (user_id, source_type),
            )
            rows = cursor.fetchall()
        return {str(row[0]) for row in rows}
