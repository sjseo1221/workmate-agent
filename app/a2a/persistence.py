"""A2A Task와 Workflow 복구 상태의 영속 경계.

개발·Contract Test에서는 표준 라이브러리 SQLite 파일을 사용하고, 운영
환경에서는 같은 Repository 계약을 PostgreSQL Adapter로 교체할 수 있도록
구성한다. 업무 도메인 테이블은 이 모듈에서 만들지 않는다.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from a2a.server.context import ServerCallContext
from a2a.server.owner_resolver import resolve_user_scope
from a2a.server.tasks import TaskStore
from a2a.types import ListTasksRequest, ListTasksResponse, Task, TaskState
from google.protobuf import json_format

try:
    from psycopg import AsyncConnection
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - optional only for SQLite tests
    AsyncConnection = None  # type: ignore[assignment]
    Jsonb = None  # type: ignore[assignment]


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS a2a_tasks (
    task_id TEXT PRIMARY KEY,
    context_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    state TEXT NOT NULL,
    task_json TEXT NOT NULL,
    thread_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT
);
CREATE TABLE IF NOT EXISTS a2a_messages (
    message_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES a2a_tasks(task_id) ON DELETE CASCADE,
    request_hash TEXT NOT NULL,
    response_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT
);
CREATE TABLE IF NOT EXISTS a2a_artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES a2a_tasks(task_id) ON DELETE CASCADE,
    artifact_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT
);
CREATE TABLE IF NOT EXISTS a2a_checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    checkpoint_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (thread_id, checkpoint_id)
);
CREATE INDEX IF NOT EXISTS a2a_tasks_state_idx ON a2a_tasks(state);
CREATE INDEX IF NOT EXISTS a2a_tasks_updated_idx ON a2a_tasks(updated_at);
"""


def task_to_dict(task: Task) -> dict[str, Any]:
    """SDK Task를 저장 가능한 JSON 객체로 변환한다."""

    return json_format.MessageToDict(task, preserving_proto_field_name=False)


def task_from_dict(value: dict[str, Any]) -> Task:
    """저장된 JSON 객체를 SDK Task로 복원한다."""

    task = Task()
    json_format.ParseDict(value, task)
    return task


def _owner(context: ServerCallContext) -> str:
    """SDK 호출 Context에서 사용자 범위를 읽고 비어 있으면 서비스 범위를 쓴다."""

    value = resolve_user_scope(context)
    return value or "service"


class PersistentTaskStore(TaskStore):
    """SDK TaskStore와 A2A 인프라 부가 저장을 함께 제공하는 SQLite Store."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        self._lock = threading.RLock()
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize_sync()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize_sync(self) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.executescript(SQLITE_SCHEMA)

    @staticmethod
    def _state(task: Task) -> str:
        return TaskState.Name(task.status.state)

    async def save(self, task: Task, context: ServerCallContext) -> None:
        """Task Snapshot을 사용자 범위와 함께 멱등 저장한다."""

        await asyncio.to_thread(self._save_sync, task, _owner(context))

    def _save_sync(self, task: Task, owner: str) -> None:
        payload = json.dumps(task_to_dict(task), ensure_ascii=False, sort_keys=True)
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO a2a_tasks
                    (task_id, context_id, owner, state, task_json, thread_id, idempotency_key)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    context_id=excluded.context_id,
                    owner=excluded.owner,
                    state=excluded.state,
                    task_json=excluded.task_json,
                    thread_id=excluded.thread_id,
                    idempotency_key=excluded.idempotency_key,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    task.id,
                    task.context_id,
                    owner,
                    self._state(task),
                    payload,
                    task.id,
                    task.id,
                ),
            )

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        """사용자 범위에 맞는 Task Snapshot을 복원한다."""

        owner = _owner(context)
        return await asyncio.to_thread(self._get_sync, task_id, owner)

    def _get_sync(self, task_id: str, owner: str) -> Task | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT task_json FROM a2a_tasks WHERE task_id = ? AND owner = ?",
                (task_id, owner),
            ).fetchone()
        return task_from_dict(json.loads(row[0])) if row else None

    async def list(
        self, params: ListTasksRequest, context: ServerCallContext
    ) -> ListTasksResponse:
        """사용자 범위와 SDK 상태 필터를 적용해 Task를 나열한다."""

        owner = _owner(context)
        rows = await asyncio.to_thread(self._list_sync, params, owner)
        response = ListTasksResponse(page_size=len(rows), total_size=len(rows))
        response.tasks.extend(rows)
        return response

    def _list_sync(self, params: ListTasksRequest, owner: str) -> list[Task]:
        query = "SELECT task_json FROM a2a_tasks WHERE owner = ?"
        values: list[object] = [owner]
        if params.context_id:
            query += " AND context_id = ?"
            values.append(params.context_id)
        if params.status:
            query += " AND state = ?"
            values.append(TaskState.Name(params.status))
        query += " ORDER BY updated_at DESC, task_id DESC"
        if params.page_size:
            query += " LIMIT ?"
            values.append(params.page_size)
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(query, values).fetchall()
        return [task_from_dict(json.loads(row[0])) for row in rows]

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        """사용자 범위에 맞는 Task와 종속 인프라 상태를 삭제한다."""

        owner = _owner(context)
        await asyncio.to_thread(self._delete_sync, task_id, owner)

    def _delete_sync(self, task_id: str, owner: str) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                "DELETE FROM a2a_tasks WHERE task_id = ? AND owner = ?",
                (task_id, owner),
            )

    async def record_message(
        self,
        message_id: str,
        task_id: str,
        payload: object,
        response: Task | None = None,
    ) -> bool:
        """Message 멱등성 Key를 기록하고 최초 요청 여부를 반환한다."""

        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        response_json = (
            json.dumps(task_to_dict(response), ensure_ascii=False, sort_keys=True)
            if response
            else None
        )
        return await asyncio.to_thread(
            self._record_message_sync,
            message_id,
            task_id,
            digest,
            response_json,
        )

    def _record_message_sync(
        self, message_id: str, task_id: str, digest: str, response_json: str | None
    ) -> bool:
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT request_hash FROM a2a_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError("message_id was reused with a different payload")
                return False
            connection.execute(
                """
                INSERT INTO a2a_messages(message_id, task_id, request_hash, response_json)
                VALUES (?, ?, ?, ?)
                """,
                (message_id, task_id, digest, response_json),
            )
            connection.execute(
                "UPDATE a2a_tasks SET idempotency_key = ? WHERE task_id = ?",
                (message_id, task_id),
            )
            return True

    async def save_artifact(self, task_id: str, artifact: object) -> None:
        """Task Artifact를 최소 7일 보존 가능한 저장 레코드로 기록한다."""

        artifact_id = getattr(artifact, "artifact_id", "") or "artifact"
        payload = json.dumps(
            json_format.MessageToDict(artifact, preserving_proto_field_name=False),
            ensure_ascii=False,
            sort_keys=True,
        )
        await asyncio.to_thread(self._save_artifact_sync, artifact_id, task_id, payload)

    def _save_artifact_sync(self, artifact_id: str, task_id: str, payload: str) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO a2a_artifacts(artifact_id, task_id, artifact_json)
                VALUES (?, ?, ?)
                ON CONFLICT(artifact_id) DO UPDATE SET artifact_json=excluded.artifact_json
                """,
                (artifact_id, task_id, payload),
            )

    async def save_checkpoint(
        self, thread_id: str, checkpoint_id: str, state: dict[str, object], metadata: dict[str, object]
    ) -> None:
        """Workflow 재개용 Checkpoint State를 저장한다."""

        await asyncio.to_thread(
            self._save_checkpoint_sync,
            thread_id,
            checkpoint_id,
            json.dumps(state, ensure_ascii=False, sort_keys=True),
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        )

    def _save_checkpoint_sync(
        self, thread_id: str, checkpoint_id: str, state: str, metadata: str
    ) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO a2a_checkpoints(thread_id, checkpoint_id, checkpoint_json, metadata_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(thread_id, checkpoint_id) DO UPDATE SET
                    checkpoint_json=excluded.checkpoint_json,
                    metadata_json=excluded.metadata_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (thread_id, checkpoint_id, state, metadata),
            )

    async def mark_cancel_requested(self, task_id: str) -> None:
        """취소 요청을 별도 상태로 기록해 재시작 뒤에도 취소 의사를 보존한다."""

        await asyncio.to_thread(self._mark_cancel_requested_sync, task_id)

    def _mark_cancel_requested_sync(self, task_id: str) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE a2a_tasks SET state='TASK_STATE_CANCELED', updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
                (task_id,),
            )


class PostgresTaskStore(TaskStore):
    """PostgreSQL 운영용 A2A Task Store.

    `migrations/001_a2a_infrastructure.sql`을 최초 연결 시 적용한다. 업무
    도메인 Repository는 이 클래스에 추가하지 않으며 M1.1에서 별도로 만든다.
    """

    def __init__(self, dsn: str) -> None:
        if AsyncConnection is None or Jsonb is None:
            raise RuntimeError("psycopg is required for DATABASE_URL")
        self.dsn = dsn
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """A2A 인프라 Migration을 한 번만 적용한다."""

        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            sql = (
                Path(__file__).resolve().parents[2]
                / "migrations"
                / "001_a2a_infrastructure.sql"
            ).read_text(encoding="utf-8")
            async with await AsyncConnection.connect(self.dsn) as connection:
                async with connection.cursor() as cursor:
                    for statement in sql.split(";"):
                        if statement.strip():
                            await cursor.execute(statement)
                await connection.commit()
            self._initialized = True

    async def check_ready(self) -> bool:
        """PostgreSQL 연결과 Migration 적용 가능 여부를 확인한다."""

        try:
            await self.initialize()
            return True
        except Exception:
            return False

    async def save(self, task: Task, context: ServerCallContext) -> None:
        """Task Snapshot을 PostgreSQL에 멱등 저장한다."""

        await self.initialize()
        task_json = Jsonb(task_to_dict(task))
        async with await AsyncConnection.connect(self.dsn) as connection:
            await connection.execute(
                """
                INSERT INTO a2a_tasks
                    (task_id, context_id, owner, state, task_json, thread_id, idempotency_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(task_id) DO UPDATE SET
                    context_id=EXCLUDED.context_id, owner=EXCLUDED.owner,
                    state=EXCLUDED.state, task_json=EXCLUDED.task_json,
                    thread_id=EXCLUDED.thread_id, idempotency_key=EXCLUDED.idempotency_key,
                    updated_at=now()
                """,
                (
                    task.id,
                    task.context_id,
                    _owner(context),
                    TaskState.Name(task.status.state),
                    task_json,
                    task.id,
                    task.id,
                ),
            )
            await connection.commit()

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        """사용자 범위에 맞는 PostgreSQL Task Snapshot을 복원한다."""

        await self.initialize()
        async with await AsyncConnection.connect(self.dsn) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT task_json FROM a2a_tasks WHERE task_id=%s AND owner=%s",
                    (task_id, _owner(context)),
                )
                row = await cursor.fetchone()
        return task_from_dict(row[0]) if row else None

    async def list(
        self, params: ListTasksRequest, context: ServerCallContext
    ) -> ListTasksResponse:
        """사용자 범위와 SDK 상태 필터를 적용해 PostgreSQL Task를 나열한다."""

        await self.initialize()
        clauses = ["owner=%s"]
        values: list[object] = [_owner(context)]
        if params.context_id:
            clauses.append("context_id=%s")
            values.append(params.context_id)
        if params.status:
            clauses.append("state=%s")
            values.append(TaskState.Name(params.status))
        query = (
            "SELECT task_json FROM a2a_tasks WHERE "
            + " AND ".join(clauses)
            + " ORDER BY updated_at DESC, task_id DESC"
        )
        if params.page_size:
            query += " LIMIT %s"
            values.append(params.page_size)
        async with await AsyncConnection.connect(self.dsn) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(query, values)
                rows = await cursor.fetchall()
        tasks = [task_from_dict(row[0]) for row in rows]
        response = ListTasksResponse(page_size=len(tasks), total_size=len(tasks))
        response.tasks.extend(tasks)
        return response

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        """사용자 범위에 맞는 Task와 종속 상태를 삭제한다."""

        await self.initialize()
        async with await AsyncConnection.connect(self.dsn) as connection:
            await connection.execute(
                "DELETE FROM a2a_tasks WHERE task_id=%s AND owner=%s",
                (task_id, _owner(context)),
            )
            await connection.commit()

    async def record_message(
        self, message_id: str, task_id: str, payload: object, response: Task | None = None
    ) -> bool:
        """Message 멱등성 Key를 PostgreSQL에 기록한다."""

        await self.initialize()
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        response_json = Jsonb(task_to_dict(response)) if response else None
        async with await AsyncConnection.connect(self.dsn) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT request_hash FROM a2a_messages WHERE message_id=%s",
                    (message_id,),
                )
                row = await cursor.fetchone()
                if row:
                    if row[0] != digest:
                        raise ValueError("message_id was reused with a different payload")
                    return False
                await cursor.execute(
                    """
                    INSERT INTO a2a_messages(message_id, task_id, request_hash, response_json)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (message_id, task_id, digest, response_json),
                )
                await cursor.execute(
                    "UPDATE a2a_tasks SET idempotency_key=%s WHERE task_id=%s",
                    (message_id, task_id),
                )
            await connection.commit()
        return True

    async def save_artifact(self, task_id: str, artifact: object) -> None:
        """Artifact Snapshot을 PostgreSQL에 저장한다."""

        await self.initialize()
        artifact_id = getattr(artifact, "artifact_id", "") or "artifact"
        payload = Jsonb(
            json_format.MessageToDict(artifact, preserving_proto_field_name=False)
        )
        async with await AsyncConnection.connect(self.dsn) as connection:
            await connection.execute(
                """
                INSERT INTO a2a_artifacts(artifact_id, task_id, artifact_json)
                VALUES (%s, %s, %s)
                ON CONFLICT(artifact_id) DO UPDATE SET artifact_json=EXCLUDED.artifact_json
                """,
                (artifact_id, task_id, payload),
            )
            await connection.commit()

    async def save_checkpoint(
        self, thread_id: str, checkpoint_id: str, state: dict[str, object], metadata: dict[str, object]
    ) -> None:
        """Workflow Checkpoint State를 PostgreSQL에 저장한다."""

        await self.initialize()
        async with await AsyncConnection.connect(self.dsn) as connection:
            await connection.execute(
                """
                INSERT INTO a2a_checkpoints(thread_id, checkpoint_id, checkpoint_json, metadata_json)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(thread_id, checkpoint_id) DO UPDATE SET
                    checkpoint_json=EXCLUDED.checkpoint_json,
                    metadata_json=EXCLUDED.metadata_json,
                    updated_at=now()
                """,
                (thread_id, checkpoint_id, Jsonb(state), Jsonb(metadata)),
            )
            await connection.commit()

    async def mark_cancel_requested(self, task_id: str) -> None:
        """취소 요청을 PostgreSQL Task Snapshot에 먼저 기록한다."""

        await self.initialize()
        async with await AsyncConnection.connect(self.dsn) as connection:
            await connection.execute(
                "UPDATE a2a_tasks SET state='TASK_STATE_CANCELED', updated_at=now() WHERE task_id=%s",
                (task_id,),
            )
            await connection.commit()
