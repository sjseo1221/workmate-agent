"""Task 업무 Repository 경계.

M1.1-02에서는 저장소 계약과 SQLite 테스트 Adapter를 제공한다. 운영
PostgreSQL 연결은 같은 계약을 사용하는 별도 Adapter·통합 검수에서 다룬다.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import sqlite3
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.domain.task import TaskRecord, TaskSourceType, TaskStatus

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional in SQLite-only test environments
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]


class TaskRepository(Protocol):
    """사용자 범위·중복·Soft Delete를 보장하는 Task 저장 계약."""

    def create(self, task: TaskRecord) -> TaskRecord: ...

    def get(self, task_id: str, user_id: str, *, include_deleted: bool = False) -> TaskRecord | None: ...

    def list(self, user_id: str, *, include_deleted: bool = False) -> list[TaskRecord]: ...

    def update(
        self,
        task_id: str,
        user_id: str,
        *,
        title: str | None = None,
        status: TaskStatus | None = None,
        priority_hint: int | None = None,
        due_at: datetime | None = None,
    ) -> TaskRecord | None: ...

    def soft_delete(self, task_id: str, user_id: str) -> bool: ...


class SQLiteTaskRepository:
    """Task Repository 계약을 검증하는 SQLite Adapter.

    이 Adapter는 단위·계약 테스트와 로컬 개발용이며 운영 PostgreSQL의
    대체 원장으로 사용하지 않는다. 모든 조회 SQL은 user_id를 조건에 포함한다.
    """

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        self.database_path = str(database_path)
        self._use_uri = self.database_path == ":memory:"
        self._connection_target = (
            f"file:workmate-task-repository-{uuid4().hex}?mode=memory&cache=shared"
            if self._use_uri
            else self.database_path
        )
        self._anchor = (
            sqlite3.connect(self._connection_target, uri=True)
            if self._use_uri
            else None
        )
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._connection_target, uri=self._use_uri)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    timezone TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    assignee_user_id TEXT NOT NULL REFERENCES users(user_id),
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority_hint INTEGER,
                    due_at TEXT,
                    source_type TEXT NOT NULL,
                    source_id TEXT,
                    deleted_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (priority_hint IS NULL OR priority_hint BETWEEN 0 AND 10),
                    CHECK ((source_type = 'manual' AND source_id IS NULL) OR
                          (source_type <> 'manual' AND source_id IS NOT NULL))
                );
                CREATE UNIQUE INDEX IF NOT EXISTS tasks_source_unique_idx
                    ON tasks (assignee_user_id, source_type, source_id)
                    WHERE source_id IS NOT NULL;
                """
            )

    @staticmethod
    def _timestamp(value: datetime | None) -> str | None:
        return value.astimezone(timezone.utc).isoformat() if value else None

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    @classmethod
    def _record(cls, row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            assignee_user_id=row["assignee_user_id"],
            title=row["title"],
            status=row["status"],
            priority_hint=row["priority_hint"],
            due_at=cls._parse_timestamp(row["due_at"]),
            source_type=row["source_type"],
            source_id=row["source_id"],
            deleted_at=cls._parse_timestamp(row["deleted_at"]),
            created_at=cls._parse_timestamp(row["created_at"]),
            updated_at=cls._parse_timestamp(row["updated_at"]),
        )

    def _ensure_user(self, connection: sqlite3.Connection, user_id: str) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO users(user_id, display_name, timezone) VALUES (?, ?, ?)",
            (user_id, user_id, "Asia/Seoul"),
        )

    def create(self, task: TaskRecord) -> TaskRecord:
        """Task를 저장하고 동일 원본이면 기존 Task를 반환한다."""

        now = datetime.now(timezone.utc)
        with closing(self._connect()) as connection, connection:
            self._ensure_user(connection, task.assignee_user_id)
            if task.source_id is not None:
                existing = connection.execute(
                    """
                    SELECT * FROM tasks
                    WHERE assignee_user_id = ? AND source_type = ? AND source_id = ?
                    """,
                    (task.assignee_user_id, task.source_type, task.source_id),
                ).fetchone()
                if existing:
                    return self._record(existing)
            connection.execute(
                """
                INSERT INTO tasks
                    (task_id, assignee_user_id, title, status, priority_hint,
                     due_at, source_type, source_id, deleted_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.assignee_user_id,
                    task.title,
                    task.status,
                    task.priority_hint,
                    self._timestamp(task.due_at),
                    task.source_type,
                    task.source_id,
                    self._timestamp(task.deleted_at),
                    self._timestamp(task.created_at or now),
                    self._timestamp(task.updated_at or now),
                ),
            )
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ? AND assignee_user_id = ?",
                (task.task_id, task.assignee_user_id),
            ).fetchone()
        assert row is not None
        return self._record(row)

    def get(self, task_id: str, user_id: str, *, include_deleted: bool = False) -> TaskRecord | None:
        """요청 사용자 소유 Task만 조회하고 기본적으로 삭제 Task를 숨긴다."""

        query = "SELECT * FROM tasks WHERE task_id = ? AND assignee_user_id = ?"
        values: list[object] = [task_id, user_id]
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        with closing(self._connect()) as connection:
            row = connection.execute(query, values).fetchone()
        return self._record(row) if row else None

    def list(self, user_id: str, *, include_deleted: bool = False) -> list[TaskRecord]:
        """요청 사용자 범위에서 등록일시(생성) 최신순으로 Task를 조회한다."""

        query = "SELECT * FROM tasks WHERE assignee_user_id = ?"
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        query += " ORDER BY created_at DESC, task_id ASC"
        with closing(self._connect()) as connection:
            rows = connection.execute(query, (user_id,)).fetchall()
        return [self._record(row) for row in rows]

    def update(
        self,
        task_id: str,
        user_id: str,
        *,
        title: str | None = None,
        status: TaskStatus | None = None,
        priority_hint: int | None = None,
        due_at: datetime | None = None,
    ) -> TaskRecord | None:
        """허용된 Task 필드만 사용자 범위 안에서 수정한다."""

        current = self.get(task_id, user_id)
        if current is None:
            return None
        candidate = TaskRecord(
            task_id=current.task_id,
            assignee_user_id=current.assignee_user_id,
            title=title if title is not None else current.title,
            status=status if status is not None else current.status,
            priority_hint=priority_hint if priority_hint is not None else current.priority_hint,
            due_at=due_at if due_at is not None else current.due_at,
            source_type=current.source_type,
            source_id=current.source_id,
            created_at=current.created_at,
            updated_at=datetime.now(timezone.utc),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE tasks
                SET title = ?, status = ?, priority_hint = ?, due_at = ?, updated_at = ?
                WHERE task_id = ? AND assignee_user_id = ? AND deleted_at IS NULL
                """,
                (
                    candidate.title,
                    candidate.status,
                    candidate.priority_hint,
                    self._timestamp(candidate.due_at),
                    self._timestamp(candidate.updated_at),
                    task_id,
                    user_id,
                ),
            )
        return self.get(task_id, user_id)

    def soft_delete(self, task_id: str, user_id: str) -> bool:
        """Task를 제거하지 않고 사용자 범위 안에서 삭제 시각만 기록한다."""

        with closing(self._connect()) as connection, connection:
            result = connection.execute(
                """
                UPDATE tasks
                SET deleted_at = ?, updated_at = ?
                WHERE task_id = ? AND assignee_user_id = ? AND deleted_at IS NULL
                """,
                (
                    self._timestamp(datetime.now(timezone.utc)),
                    self._timestamp(datetime.now(timezone.utc)),
                    task_id,
                    user_id,
                ),
            )
        return result.rowcount == 1


class PostgresTaskRepository:
    """운영 PostgreSQL용 Task Repository Adapter.

    M1.1 업무 Migration을 초기화한 뒤 모든 읽기·수정·삭제 SQL에
    assignee_user_id 조건을 포함한다. 실제 Workflow 연결은 후속 백로그에서
    수행하며, 이 클래스는 업무 저장 경계만 제공한다.
    """

    def __init__(self, dsn: str) -> None:
        if psycopg is None or dict_row is None:
            raise RuntimeError("psycopg is required for PostgresTaskRepository")
        self.dsn = dsn
        self._initialized = False

    def _connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def _initialize(self) -> None:
        if self._initialized:
            return
        migration = (
            Path(__file__).resolve().parents[2] / "migrations" / "002_work_domain.sql"
        ).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                for statement in migration.split(";"):
                    if statement.strip():
                        cursor.execute(statement)
        self._initialized = True

    @staticmethod
    def _record(row: dict[str, object]) -> TaskRecord:
        return TaskRecord(
            task_id=str(row["task_id"]),
            assignee_user_id=str(row["assignee_user_id"]),
            title=str(row["title"]),
            status=row["status"],  # type: ignore[arg-type]
            priority_hint=row["priority_hint"],  # type: ignore[arg-type]
            due_at=row["due_at"],  # type: ignore[arg-type]
            source_type=row["source_type"],  # type: ignore[arg-type]
            source_id=row["source_id"],  # type: ignore[arg-type]
            deleted_at=row["deleted_at"],  # type: ignore[arg-type]
            created_at=row["created_at"],  # type: ignore[arg-type]
            updated_at=row["updated_at"],  # type: ignore[arg-type]
        )

    def create(self, task: TaskRecord) -> TaskRecord:
        """Task를 저장하고 동일 사용자·원본이면 기존 Task를 반환한다."""

        self._initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO users(user_id, display_name, timezone)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO NOTHING
                """,
                (task.assignee_user_id, task.assignee_user_id, "Asia/Seoul"),
            )
            if task.source_id is not None:
                cursor.execute(
                    """
                    SELECT * FROM tasks
                    WHERE assignee_user_id=%s AND source_type=%s AND source_id=%s
                    """,
                    (task.assignee_user_id, task.source_type, task.source_id),
                )
                existing = cursor.fetchone()
                if existing:
                    return self._record(existing)
            cursor.execute(
                """
                INSERT INTO tasks
                    (task_id, assignee_user_id, title, status, priority_hint, due_at,
                     source_type, source_id, deleted_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()), COALESCE(%s, now()))
                RETURNING *
                """,
                (
                    task.task_id,
                    task.assignee_user_id,
                    task.title,
                    task.status,
                    task.priority_hint,
                    task.due_at,
                    task.source_type,
                    task.source_id,
                    task.deleted_at,
                    task.created_at,
                    task.updated_at,
                ),
            )
            return self._record(cursor.fetchone())

    def get(self, task_id: str, user_id: str, *, include_deleted: bool = False) -> TaskRecord | None:
        """요청 사용자 소유 Task만 PostgreSQL에서 조회한다."""

        self._initialize()
        query = "SELECT * FROM tasks WHERE task_id=%s AND assignee_user_id=%s"
        values: list[object] = [task_id, user_id]
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(query, values)
            row = cursor.fetchone()
        return self._record(row) if row else None

    def list(self, user_id: str, *, include_deleted: bool = False) -> list[TaskRecord]:
        """요청 사용자 범위의 Task를 등록일시(생성) 최신순으로 조회한다."""

        self._initialize()
        query = "SELECT * FROM tasks WHERE assignee_user_id=%s"
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        query += " ORDER BY created_at DESC, task_id ASC"
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(query, (user_id,))
            rows = cursor.fetchall()
        return [self._record(row) for row in rows]

    def update(
        self,
        task_id: str,
        user_id: str,
        *,
        title: str | None = None,
        status: TaskStatus | None = None,
        priority_hint: int | None = None,
        due_at: datetime | None = None,
    ) -> TaskRecord | None:
        """허용된 필드만 사용자 범위 안에서 수정한다."""

        self._initialize()
        current = self.get(task_id, user_id)
        if current is None:
            return None
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE tasks
                SET title=%s, status=%s, priority_hint=%s, due_at=%s, updated_at=now()
                WHERE task_id=%s AND assignee_user_id=%s AND deleted_at IS NULL
                RETURNING *
                """,
                (
                    title if title is not None else current.title,
                    status if status is not None else current.status,
                    priority_hint if priority_hint is not None else current.priority_hint,
                    due_at if due_at is not None else current.due_at,
                    task_id,
                    user_id,
                ),
            )
            row = cursor.fetchone()
        return self._record(row) if row else None

    def soft_delete(self, task_id: str, user_id: str) -> bool:
        """Task를 삭제하지 않고 사용자 범위 안에서 삭제 시각을 기록한다."""

        self._initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE tasks SET deleted_at=now(), updated_at=now()
                WHERE task_id=%s AND assignee_user_id=%s AND deleted_at IS NULL
                """,
                (task_id, user_id),
            )
            return cursor.rowcount == 1
