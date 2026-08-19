"""PrioritySnapshot 저장 계약과 SQLite/PostgreSQL Adapter."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.domain.priority import ScoreBreakdown
from app.domain.priority_snapshot import PrioritySnapshotRecord

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - SQLite-only environments
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]


class PrioritySnapshotRepository(Protocol):
    """사용자 범위와 계산 시점 멱등성을 보장하는 Snapshot 저장 계약."""

    def save_all(
        self, user_id: str, as_of: datetime, snapshots: list[PrioritySnapshotRecord]
    ) -> list[PrioritySnapshotRecord]: ...

    def latest(
        self, user_id: str, *, limit: int = 3, as_of: datetime | None = None
    ) -> list[PrioritySnapshotRecord]: ...


def _breakdown_json(breakdown: ScoreBreakdown) -> str:
    return json.dumps(
        {
            "deadline": breakdown.deadline,
            "importance": breakdown.importance,
            "blocked_or_overdue": breakdown.blocked_or_overdue,
            "meeting_commitment": breakdown.meeting_commitment,
            "calendar_relevance": breakdown.calendar_relevance,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _record_from_values(row: dict[str, object] | sqlite3.Row) -> PrioritySnapshotRecord:
    def value(name: str) -> object:
        return row[name]  # type: ignore[index]

    def json_value(name: str) -> object:
        """SQLite 문자열과 PostgreSQL JSONB 반환값을 같은 형태로 정규화한다."""

        raw = value(name)
        if isinstance(raw, (dict, list)):
            return raw
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(str(raw))

    breakdown = json_value("score_breakdown")
    reasons = tuple(json_value("reasons"))
    return PrioritySnapshotRecord(
        priority_snapshot_id=str(value("priority_snapshot_id")),
        recipient_user_id=str(value("recipient_user_id")),
        ranked_task_id=str(value("ranked_task_id")),
        calculated_at=value("calculated_at") if isinstance(value("calculated_at"), datetime) else datetime.fromisoformat(str(value("calculated_at"))),  # type: ignore[arg-type]
        as_of=value("as_of") if isinstance(value("as_of"), datetime) else datetime.fromisoformat(str(value("as_of"))),  # type: ignore[arg-type]
        rank=int(value("rank")),
        score=int(value("score")),
        score_breakdown=ScoreBreakdown(**breakdown),
        reasons=reasons,
    )


class SQLitePrioritySnapshotRepository:
    """PrioritySnapshot 계약을 검증하는 SQLite Adapter."""

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        self.database_path = str(database_path)
        self._use_uri = self.database_path == ":memory:"
        self._target = f"file:priority-{uuid4().hex}?mode=memory&cache=shared" if self._use_uri else self.database_path
        self._anchor = sqlite3.connect(self._target, uri=True) if self._use_uri else None
        if not self._use_uri:
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS priority_snapshots (
                    priority_snapshot_id TEXT PRIMARY KEY,
                    recipient_user_id TEXT NOT NULL,
                    ranked_task_id TEXT NOT NULL,
                    calculated_at TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    rank INTEGER NOT NULL CHECK (rank > 0),
                    score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 95),
                    score_breakdown TEXT NOT NULL,
                    reasons TEXT NOT NULL,
                    UNIQUE (recipient_user_id, as_of, ranked_task_id)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._target, uri=self._use_uri)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat()

    def save_all(self, user_id: str, as_of: datetime, snapshots: list[PrioritySnapshotRecord]) -> list[PrioritySnapshotRecord]:
        """동일 사용자·기준 시각의 결과를 멱등적으로 저장한다."""

        if any(snapshot.recipient_user_id != user_id or snapshot.as_of != as_of for snapshot in snapshots):
            raise ValueError("snapshot user and as_of must match save scope")
        with closing(self._connect()) as connection, connection:
            for snapshot in snapshots:
                connection.execute(
                    """
                    INSERT INTO priority_snapshots
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(recipient_user_id, as_of, ranked_task_id) DO UPDATE SET
                      priority_snapshot_id=excluded.priority_snapshot_id,
                      calculated_at=excluded.calculated_at,
                      rank=excluded.rank,
                      score=excluded.score,
                      score_breakdown=excluded.score_breakdown,
                      reasons=excluded.reasons
                    """,
                    (
                        snapshot.priority_snapshot_id,
                        snapshot.recipient_user_id,
                        snapshot.ranked_task_id,
                        self._timestamp(snapshot.calculated_at),
                        self._timestamp(snapshot.as_of),
                        snapshot.rank,
                        snapshot.score,
                        _breakdown_json(snapshot.score_breakdown),
                        json.dumps(snapshot.reasons, ensure_ascii=False),
                    ),
                )
        return self.latest(user_id, limit=len(snapshots) or 3, as_of=as_of)

    def latest(self, user_id: str, *, limit: int = 3, as_of: datetime | None = None) -> list[PrioritySnapshotRecord]:
        """요청 사용자 범위에서 최신 Snapshot을 결정적 순서로 조회한다."""

        query = "SELECT * FROM priority_snapshots WHERE recipient_user_id = ?"
        values: list[object] = [user_id]
        if as_of is not None:
            query += " AND as_of = ?"
            values.append(self._timestamp(as_of))
        query += " ORDER BY calculated_at DESC, rank ASC, ranked_task_id ASC LIMIT ?"
        values.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(query, values).fetchall()
        return [_record_from_values(row) for row in rows]


class PostgresPrioritySnapshotRepository:
    """운영 PostgreSQL용 PrioritySnapshot Adapter."""

    def __init__(self, dsn: str) -> None:
        if psycopg is None or dict_row is None:
            raise RuntimeError("psycopg is required for PostgresPrioritySnapshotRepository")
        self.dsn = dsn
        self._initialized = False

    def _connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def _initialize(self) -> None:
        if self._initialized:
            return
        migration = (Path(__file__).resolve().parents[2] / "migrations" / "003_priority_snapshots.sql").read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                for statement in migration.split(";"):
                    if statement.strip():
                        cursor.execute(statement)
        self._initialized = True

    def save_all(self, user_id: str, as_of: datetime, snapshots: list[PrioritySnapshotRecord]) -> list[PrioritySnapshotRecord]:
        """동일 사용자·기준 시각 결과를 PostgreSQL에 멱등적으로 저장한다."""

        self._initialize()
        if any(snapshot.recipient_user_id != user_id or snapshot.as_of != as_of for snapshot in snapshots):
            raise ValueError("snapshot user and as_of must match save scope")
        with self._connect() as connection, connection.cursor() as cursor:
            for snapshot in snapshots:
                cursor.execute(
                    """
                    INSERT INTO priority_snapshots
                    (priority_snapshot_id, recipient_user_id, ranked_task_id, calculated_at, as_of, rank, score, score_breakdown, reasons)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                    ON CONFLICT (recipient_user_id, as_of, ranked_task_id) DO UPDATE SET
                      priority_snapshot_id=EXCLUDED.priority_snapshot_id,
                      calculated_at=EXCLUDED.calculated_at,
                      rank=EXCLUDED.rank,
                      score=EXCLUDED.score,
                      score_breakdown=EXCLUDED.score_breakdown,
                      reasons=EXCLUDED.reasons
                    """,
                    (
                        snapshot.priority_snapshot_id,
                        snapshot.recipient_user_id,
                        snapshot.ranked_task_id,
                        snapshot.calculated_at,
                        snapshot.as_of,
                        snapshot.rank,
                        snapshot.score,
                        _breakdown_json(snapshot.score_breakdown),
                        json.dumps(snapshot.reasons, ensure_ascii=False),
                    ),
                )
        return self.latest(user_id, limit=len(snapshots) or 3, as_of=as_of)

    def latest(self, user_id: str, *, limit: int = 3, as_of: datetime | None = None) -> list[PrioritySnapshotRecord]:
        """요청 사용자 범위의 최신 Snapshot을 PostgreSQL에서 조회한다."""

        self._initialize()
        query = "SELECT * FROM priority_snapshots WHERE recipient_user_id=%s"
        values: list[object] = [user_id]
        if as_of is not None:
            query += " AND as_of=%s"
            values.append(as_of)
        query += " ORDER BY calculated_at DESC, rank ASC, ranked_task_id ASC LIMIT %s"
        values.append(limit)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(query, values)
            rows = cursor.fetchall()
        return [_record_from_values(row) for row in rows]
