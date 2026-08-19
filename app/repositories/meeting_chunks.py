"""회의 검색 Chunk 저장소.

SQLite Adapter는 단위·계약 테스트 전용이며 운영 원장은 PostgreSQL Adapter다.
모든 조회·수정 SQL은 요청 user_id를 조건에 포함한다.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Protocol
from uuid import uuid4

from app.domain.meeting_chunk import MeetingChunkRecord


@dataclass(frozen=True, slots=True)
class KeywordSearchHit:
    """pg_trgm 키워드 검색 결과와 회의 출처 메타데이터."""

    meeting_chunk_id: str
    meeting_id: str
    user_id: str
    content: str
    meeting_title: str
    meeting_started_at: datetime | None
    sequence_no: int
    similarity: float


@dataclass(frozen=True, slots=True)
class DenseSearchHit:
    """Cosine distance 기반 Dense 검색 결과."""

    meeting_chunk_id: str
    meeting_id: str
    user_id: str
    content: str
    meeting_title: str
    meeting_started_at: datetime | None
    sequence_no: int
    distance: float


@dataclass(frozen=True, slots=True)
class HybridSearchHit:
    """Dense·Trigram 결과를 RRF로 결합한 검색 결과."""

    meeting_chunk_id: str
    meeting_id: str
    user_id: str
    content: str
    meeting_title: str
    meeting_started_at: datetime | None
    sequence_no: int
    rrf_score: float
    dense_rank: int | None
    keyword_rank: int | None

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - SQLite-only environments
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]


class MeetingChunkRepository(Protocol):
    """사용자 범위·순서 멱등성을 보장하는 Chunk 저장 계약."""

    def create(self, chunk: MeetingChunkRecord) -> MeetingChunkRecord: ...

    def get(self, meeting_chunk_id: str, user_id: str) -> MeetingChunkRecord | None: ...

    def list(self, parent_meeting_id: str, user_id: str) -> list[MeetingChunkRecord]: ...

    def update(self, chunk: MeetingChunkRecord, user_id: str) -> MeetingChunkRecord | None: ...

    def search_keyword(
        self,
        query: str,
        user_id: str,
        *,
        meeting_id: str | None = None,
        started_from: datetime | None = None,
        ended_to: datetime | None = None,
        limit: int = 20,
    ) -> list[KeywordSearchHit]: ...

    def search_hybrid(
        self,
        query: str,
        query_embedding: tuple[float, ...],
        user_id: str,
        *,
        meeting_id: str | None = None,
        started_from: datetime | None = None,
        ended_to: datetime | None = None,
        limit: int = 20,
    ) -> list[HybridSearchHit]: ...


class SQLiteMeetingChunkRepository:
    """Chunk 사용자 격리와 수정 계약을 검증하는 SQLite Adapter."""

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        self.database_path = str(database_path)
        self._uri = self.database_path == ":memory:"
        self._target = f"file:meeting-chunk-{uuid4().hex}?mode=memory&cache=shared" if self._uri else self.database_path
        self._anchor = sqlite3.connect(self._target, uri=True) if self._uri else None
        if not self._uri:
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._target, uri=self._uri)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meetings (
                    meeting_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
                    UNIQUE (meeting_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS meeting_chunks (
                    meeting_chunk_id TEXT PRIMARY KEY, parent_meeting_id TEXT NOT NULL,
                    user_id TEXT NOT NULL, sequence_no INTEGER NOT NULL, speaker TEXT,
                    started_at_ms INTEGER, ended_at_ms INTEGER, content TEXT NOT NULL,
                    embedding TEXT NOT NULL, embedding_model TEXT NOT NULL, embedding_version TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(parent_meeting_id, user_id, sequence_no),
                    FOREIGN KEY(parent_meeting_id, user_id) REFERENCES meetings(meeting_id, user_id),
                    CHECK(ended_at_ms IS NULL OR started_at_ms IS NULL OR ended_at_ms >= started_at_ms)
                );
                """
            )

    @staticmethod
    def _timestamp(value: datetime | None) -> str:
        return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()

    @staticmethod
    def _record(row: sqlite3.Row) -> MeetingChunkRecord:
        return MeetingChunkRecord.with_embedding(
            meeting_chunk_id=row["meeting_chunk_id"], parent_meeting_id=row["parent_meeting_id"],
            user_id=row["user_id"], sequence_no=row["sequence_no"], content=row["content"],
            embedding=json.loads(row["embedding"]), speaker=row["speaker"],
            started_at_ms=row["started_at_ms"], ended_at_ms=row["ended_at_ms"],
            embedding_model=row["embedding_model"], embedding_version=row["embedding_version"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def create(self, chunk: MeetingChunkRecord) -> MeetingChunkRecord:
        """회의가 존재하는 사용자 범위에 Chunk를 멱등 저장한다."""
        with closing(self._connect()) as connection, connection:
            connection.execute("INSERT OR IGNORE INTO meetings(meeting_id,user_id,title) VALUES (?,?,?)", (chunk.parent_meeting_id, chunk.user_id, "회의"))
            connection.execute(
                """INSERT OR IGNORE INTO meeting_chunks
                (meeting_chunk_id,parent_meeting_id,user_id,sequence_no,speaker,started_at_ms,ended_at_ms,content,embedding,embedding_model,embedding_version,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (chunk.meeting_chunk_id, chunk.parent_meeting_id, chunk.user_id, chunk.sequence_no, chunk.speaker, chunk.started_at_ms, chunk.ended_at_ms, chunk.content, json.dumps(chunk.embedding), chunk.embedding_model, chunk.embedding_version, self._timestamp(chunk.created_at)),
            )
            row = connection.execute("SELECT * FROM meeting_chunks WHERE parent_meeting_id=? AND user_id=? AND sequence_no=?", (chunk.parent_meeting_id, chunk.user_id, chunk.sequence_no)).fetchone()
        assert row is not None
        return self._record(row)

    def get(self, meeting_chunk_id: str, user_id: str) -> MeetingChunkRecord | None:
        """요청 사용자 소유 Chunk만 조회한다."""
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM meeting_chunks WHERE meeting_chunk_id=? AND user_id=?", (meeting_chunk_id, user_id)).fetchone()
        return self._record(row) if row else None

    def list(self, parent_meeting_id: str, user_id: str) -> list[MeetingChunkRecord]:
        """요청 사용자 회의의 Chunk를 순서대로 반환한다."""
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM meeting_chunks WHERE parent_meeting_id=? AND user_id=? ORDER BY sequence_no", (parent_meeting_id, user_id)).fetchall()
        return [self._record(row) for row in rows]

    def update(self, chunk: MeetingChunkRecord, user_id: str) -> MeetingChunkRecord | None:
        """요청 사용자 범위에서 Chunk 원문과 Embedding을 수정한다."""
        with closing(self._connect()) as connection, connection:
            connection.execute("UPDATE meeting_chunks SET content=?, embedding=?, embedding_model=?, embedding_version=? WHERE meeting_chunk_id=? AND user_id=?", (chunk.content, json.dumps(chunk.embedding), chunk.embedding_model, chunk.embedding_version, chunk.meeting_chunk_id, user_id))
        return self.get(chunk.meeting_chunk_id, user_id)


class PostgresMeetingChunkRepository:
    """운영 PostgreSQL용 Chunk Repository Adapter."""

    def __init__(self, dsn: str) -> None:
        if psycopg is None or dict_row is None:
            raise RuntimeError("psycopg is required for PostgresMeetingChunkRepository")
        self.dsn = dsn
        self._initialized = False

    def _connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def _initialize(self) -> None:
        if self._initialized:
            return
        migration_root = Path(__file__).resolve().parents[2] / "migrations"
        with self._connect() as connection, connection.cursor() as cursor:
            for migration_name in ("002_work_domain.sql", "004_search_data.sql"):
                migration = (migration_root / migration_name).read_text(encoding="utf-8")
                for statement in migration.split(";"):
                    if statement.strip():
                        cursor.execute(statement)
        self._initialized = True

    @staticmethod
    def _vector_literal(embedding: tuple[float, ...]) -> str:
        """pgvector Adapter 의존성 없이 Vector 입력 문자열을 생성한다."""
        return "[" + ",".join(format(value, ".17g") for value in embedding) + "]"

    @staticmethod
    def _embedding(value: object) -> tuple[float, ...]:
        """psycopg가 반환한 pgvector 문자열 또는 Sequence를 Tuple로 변환한다."""
        if isinstance(value, str):
            return tuple(float(item) for item in value.strip("[]").split(",") if item)
        return tuple(float(item) for item in value)  # type: ignore[arg-type]

    @staticmethod
    def _record(row: dict[str, object]) -> MeetingChunkRecord:
        return MeetingChunkRecord.with_embedding(
            meeting_chunk_id=str(row["meeting_chunk_id"]), parent_meeting_id=str(row["parent_meeting_id"]), user_id=str(row["user_id"]), sequence_no=int(row["sequence_no"]), content=str(row["content"]), embedding=PostgresMeetingChunkRepository._embedding(row["embedding"]), speaker=row["speaker"], started_at_ms=row["started_at_ms"], ended_at_ms=row["ended_at_ms"], embedding_model=str(row["embedding_model"]), embedding_version=str(row["embedding_version"]), created_at=row["created_at"],
        )

    def create(self, chunk: MeetingChunkRecord) -> MeetingChunkRecord:
        """회의가 존재하는 사용자 범위에 Chunk를 멱등 저장한다.

        `meetings` 행은 `title`/`started_at`/`ended_at`을 `chunk.meeting_*`에서 채운다 —
        예전엔 항상 `title="회의"`·`started_at=NULL`로 고정 삽입해서, 검색 결과가 이 값을
        인용할 때(`meeting_started_at is None`) `search_meetings`가 항상 `ValueError`를
        내던 버그가 있었다(2026-08-16, 14번 갭 문서). `DO UPDATE`로 매번 최신 값을
        덮어써 이전에 잘못 저장된 행도 재색인 시 자동으로 복구된다(자체 치유).
        """
        self._initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO users(user_id,display_name,timezone) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (chunk.user_id, chunk.user_id, "Asia/Seoul"))
            cursor.execute(
                """INSERT INTO meetings(meeting_id,user_id,title,started_at,ended_at) VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (meeting_id) DO UPDATE SET title=EXCLUDED.title, started_at=EXCLUDED.started_at, ended_at=EXCLUDED.ended_at""",
                (chunk.parent_meeting_id, chunk.user_id, chunk.meeting_title or "회의", chunk.meeting_started_at, chunk.meeting_ended_at),
            )
            cursor.execute("""INSERT INTO meeting_chunks(meeting_chunk_id,parent_meeting_id,user_id,sequence_no,speaker,started_at_ms,ended_at_ms,content,embedding,embedding_model,embedding_version)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s) ON CONFLICT(parent_meeting_id,user_id,sequence_no) DO NOTHING RETURNING *""", (chunk.meeting_chunk_id, chunk.parent_meeting_id, chunk.user_id, chunk.sequence_no, chunk.speaker, chunk.started_at_ms, chunk.ended_at_ms, chunk.content, self._vector_literal(chunk.embedding), chunk.embedding_model, chunk.embedding_version))
            row = cursor.fetchone()
            if row is None:
                cursor.execute("SELECT * FROM meeting_chunks WHERE parent_meeting_id=%s AND user_id=%s AND sequence_no=%s", (chunk.parent_meeting_id, chunk.user_id, chunk.sequence_no))
                row = cursor.fetchone()
        assert row is not None
        return self._record(row)

    def get(self, meeting_chunk_id: str, user_id: str) -> MeetingChunkRecord | None:
        """요청 사용자 소유 Chunk만 조회한다."""
        self._initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM meeting_chunks WHERE meeting_chunk_id=%s AND user_id=%s", (meeting_chunk_id, user_id))
            row = cursor.fetchone()
        return self._record(row) if row else None

    def list(self, parent_meeting_id: str, user_id: str) -> list[MeetingChunkRecord]:
        """요청 사용자 회의의 Chunk를 순서대로 반환한다."""
        self._initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM meeting_chunks WHERE parent_meeting_id=%s AND user_id=%s ORDER BY sequence_no", (parent_meeting_id, user_id))
            rows = cursor.fetchall()
        return [self._record(row) for row in rows]

    def update(self, chunk: MeetingChunkRecord, user_id: str) -> MeetingChunkRecord | None:
        """요청 사용자 범위에서 Chunk 원문과 Embedding을 수정한다."""
        self._initialize()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE meeting_chunks SET content=%s, embedding=%s::vector, embedding_model=%s, embedding_version=%s WHERE meeting_chunk_id=%s AND user_id=%s", (chunk.content, self._vector_literal(chunk.embedding), chunk.embedding_model, chunk.embedding_version, chunk.meeting_chunk_id, user_id))
        return self.get(chunk.meeting_chunk_id, user_id)

    def search_keyword(
        self,
        query: str,
        user_id: str,
        *,
        meeting_id: str | None = None,
        started_from: datetime | None = None,
        ended_to: datetime | None = None,
        limit: int = 20,
    ) -> list[KeywordSearchHit]:
        """pg_trgm으로 사용자 범위의 회의 Chunk를 검색한다.

        `meeting_id`와 회의 시작일 범위를 선택적으로 적용하며, 결과는
        similarity 내림차순과 Chunk 순서로 결정적으로 정렬한다.
        """
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("query must not be empty")
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        self._initialize()
        clauses = ["c.user_id = %s", "c.content %% %s"]
        params: list[object] = [user_id, normalized_query]
        if meeting_id is not None:
            clauses.append("c.parent_meeting_id = %s")
            params.append(meeting_id)
        if started_from is not None:
            clauses.append("m.started_at >= %s")
            params.append(started_from)
        if ended_to is not None:
            clauses.append("m.started_at <= %s")
            params.append(ended_to)
        params = [normalized_query, *params, limit]
        sql = f"""
            SELECT c.meeting_chunk_id, c.parent_meeting_id, c.user_id,
                   c.content, c.sequence_no, similarity(c.content, %s) AS similarity,
                   m.title AS meeting_title, m.started_at AS meeting_started_at
            FROM meeting_chunks AS c
            JOIN meetings AS m
              ON m.meeting_id = c.parent_meeting_id AND m.user_id = c.user_id
            WHERE {' AND '.join(clauses)}
            ORDER BY similarity DESC, c.sequence_no ASC, c.meeting_chunk_id ASC
            LIMIT %s
        """
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        return [
            KeywordSearchHit(
                meeting_chunk_id=str(row["meeting_chunk_id"]),
                meeting_id=str(row["parent_meeting_id"]),
                user_id=str(row["user_id"]),
                content=str(row["content"]),
                meeting_title=str(row["meeting_title"]),
                meeting_started_at=row["meeting_started_at"],
                sequence_no=int(row["sequence_no"]),
                similarity=float(row["similarity"]),
            )
            for row in rows
        ]

    def search_dense(
        self,
        query_embedding: tuple[float, ...],
        user_id: str,
        *,
        meeting_id: str | None = None,
        started_from: datetime | None = None,
        ended_to: datetime | None = None,
        limit: int = 20,
    ) -> list[DenseSearchHit]:
        """Cosine distance로 사용자 범위의 Dense 후보를 조회한다."""
        if len(query_embedding) != 1536:
            raise ValueError("query_embedding must have exactly 1536 dimensions")
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        self._initialize()
        clauses = ["c.user_id = %s"]
        filter_params: list[object] = [user_id]
        if meeting_id is not None:
            clauses.append("c.parent_meeting_id = %s")
            filter_params.append(meeting_id)
        if started_from is not None:
            clauses.append("m.started_at >= %s")
            filter_params.append(started_from)
        if ended_to is not None:
            clauses.append("m.started_at <= %s")
            filter_params.append(ended_to)
        vector_literal = self._vector_literal(query_embedding)
        sql = f"""
            SELECT c.meeting_chunk_id, c.parent_meeting_id, c.user_id,
                   c.content, c.sequence_no, c.embedding <=> %s::vector AS distance,
                   m.title AS meeting_title, m.started_at AS meeting_started_at
            FROM meeting_chunks AS c
            JOIN meetings AS m
              ON m.meeting_id = c.parent_meeting_id AND m.user_id = c.user_id
            WHERE {' AND '.join(clauses)}
            ORDER BY distance ASC, c.meeting_chunk_id ASC
            LIMIT %s
        """
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(sql, [vector_literal, *filter_params, limit])
            rows = cursor.fetchall()
        return [
            DenseSearchHit(
                meeting_chunk_id=str(row["meeting_chunk_id"]),
                meeting_id=str(row["parent_meeting_id"]),
                user_id=str(row["user_id"]),
                content=str(row["content"]),
                meeting_title=str(row["meeting_title"]),
                meeting_started_at=row["meeting_started_at"],
                sequence_no=int(row["sequence_no"]),
                distance=float(row["distance"]),
            )
            for row in rows
        ]

    def search_hybrid(
        self,
        query: str,
        query_embedding: tuple[float, ...],
        user_id: str,
        *,
        meeting_id: str | None = None,
        started_from: datetime | None = None,
        ended_to: datetime | None = None,
        limit: int = 20,
    ) -> list[HybridSearchHit]:
        """Dense와 Trigram 후보를 RRF(k=60)로 결합해 반환한다."""
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        candidate_limit = min(max(limit * 2, 20), 100)
        dense = self.search_dense(
            query_embedding, user_id, meeting_id=meeting_id,
            started_from=started_from, ended_to=ended_to, limit=candidate_limit,
        )
        keyword = self.search_keyword(
            query, user_id, meeting_id=meeting_id,
            started_from=started_from, ended_to=ended_to, limit=candidate_limit,
        )
        combined: dict[str, dict[str, object]] = {}
        for rank, hit in enumerate(dense, start=1):
            combined.setdefault(hit.meeting_chunk_id, {"hit": hit, "dense_rank": rank, "keyword_rank": None})["dense_rank"] = rank
        for rank, hit in enumerate(keyword, start=1):
            item = combined.setdefault(hit.meeting_chunk_id, {"hit": hit, "dense_rank": None, "keyword_rank": rank})
            item["keyword_rank"] = rank
            if "hit" not in item or isinstance(item["hit"], KeywordSearchHit):
                item["hit"] = hit
        results: list[HybridSearchHit] = []
        for item in combined.values():
            hit = item["hit"]
            dense_rank = item["dense_rank"]
            keyword_rank = item["keyword_rank"]
            assert isinstance(hit, (DenseSearchHit, KeywordSearchHit))
            results.append(
                HybridSearchHit(
                    meeting_chunk_id=hit.meeting_chunk_id,
                    meeting_id=hit.meeting_id,
                    user_id=hit.user_id,
                    content=hit.content,
                    meeting_title=hit.meeting_title,
                    meeting_started_at=hit.meeting_started_at,
                    sequence_no=hit.sequence_no,
                    rrf_score=(1 / (60 + dense_rank) if isinstance(dense_rank, int) else 0)
                    + (1 / (60 + keyword_rank) if isinstance(keyword_rank, int) else 0),
                    dense_rank=dense_rank if isinstance(dense_rank, int) else None,
                    keyword_rank=keyword_rank if isinstance(keyword_rank, int) else None,
                )
            )
        results.sort(key=lambda hit: (-hit.rrf_score, hit.sequence_no, hit.meeting_chunk_id))
        return results[:limit]
