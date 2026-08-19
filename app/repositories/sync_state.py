"""Gmail·Google Calendar 증분 동기화 상태 Repository."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import sqlite3
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.domain.sync_state import SyncStateRecord, SyncSourceType

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - SQLite-only 환경
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]


class SyncStateRepository(Protocol):
    """사용자별 Cursor·Watch 상태 저장 계약."""

    def upsert(self, state: SyncStateRecord) -> SyncStateRecord: ...

    def get(self, user_id: str, source_type: SyncSourceType) -> SyncStateRecord | None: ...

    def list(self, user_id: str) -> list[SyncStateRecord]: ...


class SQLiteSyncStateRepository:
    """단위·계약 테스트와 로컬 개발용 동기화 상태 Adapter."""

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        self.database_path = str(database_path)
        self._use_uri = self.database_path == ":memory:"
        self._connection_target = (
            f"file:workmate-sync-state-{uuid4().hex}?mode=memory&cache=shared"
            if self._use_uri
            else self.database_path
        )
        self._anchor = (
            sqlite3.connect(self._connection_target, uri=True)
            if self._use_uri
            else None
        )
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
                CREATE TABLE IF NOT EXISTS source_sync_states (
                    source_sync_state_id TEXT PRIMARY KEY,
                    sync_user_id TEXT NOT NULL,
                    source_type TEXT NOT NULL CHECK (source_type IN ('gmail', 'google_calendar')),
                    sync_cursor TEXT,
                    channel_id TEXT,
                    channel_token TEXT,
                    resource_id TEXT,
                    watch_expiration TEXT,
                    last_synced_at TEXT,
                    status TEXT NOT NULL CHECK (status IN ('idle', 'running', 'succeeded', 'failed')),
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (sync_user_id, source_type)
                );
                """
            )

    @staticmethod
    def _timestamp(value: datetime | None) -> str | None:
        return value.astimezone(timezone.utc).isoformat() if value else None

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    @classmethod
    def _record(cls, row: sqlite3.Row) -> SyncStateRecord:
        return SyncStateRecord(
            source_sync_state_id=row["source_sync_state_id"],
            sync_user_id=row["sync_user_id"],
            source_type=row["source_type"],  # type: ignore[arg-type]
            sync_cursor=row["sync_cursor"],
            channel_id=row["channel_id"],
            channel_token=row["channel_token"],
            resource_id=row["resource_id"],
            watch_expiration=cls._parse_timestamp(row["watch_expiration"]),
            last_synced_at=cls._parse_timestamp(row["last_synced_at"]),
            status=row["status"],  # type: ignore[arg-type]
            error_message=row["error_message"],
            created_at=cls._parse_timestamp(row["created_at"]),
            updated_at=cls._parse_timestamp(row["updated_at"]),
        )

    def upsert(self, state: SyncStateRecord) -> SyncStateRecord:
        """동일 사용자·소스의 Cursor 상태를 원자적으로 갱신한다."""

        now = datetime.now(timezone.utc)
        created = self._timestamp(state.created_at or now)
        updated = self._timestamp(state.updated_at or now)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO source_sync_states
                    (source_sync_state_id, sync_user_id, source_type, sync_cursor,
                     channel_id, channel_token, resource_id, watch_expiration,
                     last_synced_at, status, error_message, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (sync_user_id, source_type) DO UPDATE SET
                    sync_cursor=excluded.sync_cursor, channel_id=excluded.channel_id,
                    channel_token=excluded.channel_token, resource_id=excluded.resource_id,
                    watch_expiration=excluded.watch_expiration, last_synced_at=excluded.last_synced_at,
                    status=excluded.status, error_message=excluded.error_message,
                    updated_at=excluded.updated_at
                """,
                (state.source_sync_state_id, state.sync_user_id, state.source_type,
                 state.sync_cursor, state.channel_id, state.channel_token, state.resource_id,
                 self._timestamp(state.watch_expiration), self._timestamp(state.last_synced_at),
                 state.status, state.error_message, created, updated),
            )
            row = connection.execute(
                "SELECT * FROM source_sync_states WHERE sync_user_id=? AND source_type=?",
                (state.sync_user_id, state.source_type),
            ).fetchone()
        assert row is not None
        return self._record(row)

    def get(self, user_id: str, source_type: SyncSourceType) -> SyncStateRecord | None:
        """요청 사용자와 소스가 일치하는 상태만 반환한다."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM source_sync_states WHERE sync_user_id=? AND source_type=?",
                (user_id, source_type),
            ).fetchone()
        return self._record(row) if row else None

    def list(self, user_id: str) -> list[SyncStateRecord]:
        """사용자 범위의 상태를 소스명 기준으로 결정적으로 반환한다."""

        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM source_sync_states WHERE sync_user_id=? ORDER BY source_type ASC",
                (user_id,),
            ).fetchall()
        return [self._record(row) for row in rows]


class PostgresSyncStateRepository:
    """운영 PostgreSQL용 동기화 상태 Adapter."""

    def __init__(self, dsn: str) -> None:
        if psycopg is None or dict_row is None:
            raise RuntimeError("psycopg is required for PostgresSyncStateRepository")
        self.dsn = dsn
        self._initialized = False

    def _connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def _initialize(self) -> None:
        if self._initialized:
            return
        migration = (Path(__file__).resolve().parents[2] / "migrations" / "002_work_domain.sql").read_text(encoding="utf-8")
        with self._connect() as connection, connection.cursor() as cursor:
            for statement in migration.split(";"):
                if statement.strip():
                    cursor.execute(statement)
        self._initialized = True

    def _ensure_user(self, connection, user_id: str) -> None:
        """동기화 상태 외래키가 참조할 사용자 원장 행을 보장한다."""

        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO users(user_id, display_name, timezone)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO NOTHING
                """,
                (user_id, user_id, "Asia/Seoul"),
            )

    @staticmethod
    def _record(row: dict[str, object]) -> SyncStateRecord:
        return SyncStateRecord(
            source_sync_state_id=str(row["source_sync_state_id"]),
            sync_user_id=str(row["sync_user_id"]),
            source_type=row["source_type"],  # type: ignore[arg-type]
            sync_cursor=row["sync_cursor"],  # type: ignore[arg-type]
            channel_id=row["channel_id"],  # type: ignore[arg-type]
            channel_token=row["channel_token"],  # type: ignore[arg-type]
            resource_id=row["resource_id"],  # type: ignore[arg-type]
            watch_expiration=row["watch_expiration"],  # type: ignore[arg-type]
            last_synced_at=row["last_synced_at"],  # type: ignore[arg-type]
            status=row["status"],  # type: ignore[arg-type]
            error_message=row["error_message"],  # type: ignore[arg-type]
            created_at=row["created_at"],  # type: ignore[arg-type]
            updated_at=row["updated_at"],  # type: ignore[arg-type]
        )

    def upsert(self, state: SyncStateRecord) -> SyncStateRecord:
        """동일 사용자·소스의 상태를 PostgreSQL에서 갱신한다."""

        self._initialize()
        now = datetime.now(timezone.utc)
        with self._connect() as connection, connection.cursor() as cursor:
            self._ensure_user(connection, state.sync_user_id)
            cursor.execute(
                """
                INSERT INTO source_sync_states
                    (source_sync_state_id, sync_user_id, source_type, sync_cursor,
                     channel_id, channel_token, resource_id, watch_expiration,
                     last_synced_at, status, error_message, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()), COALESCE(%s, now()))
                ON CONFLICT (sync_user_id, source_type) DO UPDATE SET
                    sync_cursor=excluded.sync_cursor, channel_id=excluded.channel_id,
                    channel_token=excluded.channel_token, resource_id=excluded.resource_id,
                    watch_expiration=excluded.watch_expiration, last_synced_at=excluded.last_synced_at,
                    status=excluded.status, error_message=excluded.error_message, updated_at=excluded.updated_at
                RETURNING *
                """,
                (state.source_sync_state_id, state.sync_user_id, state.source_type, state.sync_cursor,
                 state.channel_id, state.channel_token, state.resource_id, state.watch_expiration,
                 state.last_synced_at, state.status, state.error_message, state.created_at or now, state.updated_at or now),
            )
            row = cursor.fetchone()
        return self._record(row)

    def get(self, user_id: str, source_type: SyncSourceType) -> SyncStateRecord | None:
        """요청 사용자와 소스가 일치하는 PostgreSQL 상태만 반환한다."""

        self._initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM source_sync_states WHERE sync_user_id=%s AND source_type=%s", (user_id, source_type))
            row = cursor.fetchone()
        return self._record(row) if row else None

    def list(self, user_id: str) -> list[SyncStateRecord]:
        """사용자 범위의 동기화 상태를 소스명 순으로 반환한다."""

        self._initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM source_sync_states WHERE sync_user_id=%s ORDER BY source_type ASC", (user_id,))
            rows = cursor.fetchall()
        return [self._record(row) for row in rows]
