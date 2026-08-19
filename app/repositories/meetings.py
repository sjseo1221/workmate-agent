"""회의 메타데이터와 업로드 검증 결과를 저장하는 SQLite Adapter."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import sqlite3
from pathlib import Path
from uuid import uuid4

from app.domain.meeting import MeetingRecord, RecordingRecord


class SQLiteMeetingRepository:
    """M3.1 입력 계약을 검증하고 사용자별로 격리하는 저장소."""

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        self.database_path = str(database_path)
        self._uri = self.database_path == ":memory:"
        self._target = f"file:meeting-{uuid4().hex}?mode=memory&cache=shared" if self._uri else self.database_path
        self._anchor = sqlite3.connect(self._target, uri=True) if self._uri else None
        if not self._uri:
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._target, uri=self._uri)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS meetings (
              meeting_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
              started_at TEXT, ended_at TEXT, created_at TEXT NOT NULL,
              source_audio_uri TEXT,
              UNIQUE(user_id, meeting_id)
            );
            CREATE TABLE IF NOT EXISTS recordings (
              recording_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, user_id TEXT NOT NULL,
              filename TEXT NOT NULL, content_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,
              sha256 TEXT NOT NULL, object_key TEXT, created_at TEXT NOT NULL,
              UNIQUE(user_id, meeting_id, sha256),
              FOREIGN KEY(meeting_id) REFERENCES meetings(meeting_id)
            );
            CREATE TABLE IF NOT EXISTS recording_sessions (
              meeting_id TEXT NOT NULL, user_id TEXT NOT NULL,
              last_chunk_no INTEGER NOT NULL DEFAULT -1,
              chunk_hashes TEXT NOT NULL DEFAULT '{}',
              PRIMARY KEY(meeting_id, user_id),
              FOREIGN KEY(meeting_id) REFERENCES meetings(meeting_id)
            );
            CREATE TABLE IF NOT EXISTS transcripts (
              transcript_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, user_id TEXT NOT NULL,
              chunk_no INTEGER NOT NULL, text TEXT NOT NULL, is_final INTEGER NOT NULL,
              start_ms INTEGER, end_ms INTEGER, created_at TEXT NOT NULL,
              UNIQUE(meeting_id, user_id, chunk_no, is_final),
              FOREIGN KEY(meeting_id) REFERENCES meetings(meeting_id)
            );
            CREATE TABLE IF NOT EXISTS action_items (
              action_item_id TEXT NOT NULL, meeting_id TEXT NOT NULL, user_id TEXT NOT NULL,
              title TEXT NOT NULL, evidence_text TEXT NOT NULL, approval_status TEXT NOT NULL DEFAULT 'pending',
              task_id TEXT, created_at TEXT NOT NULL,
              PRIMARY KEY (action_item_id, meeting_id),
              FOREIGN KEY(meeting_id) REFERENCES meetings(meeting_id)
            );
            """)
            # `CREATE TABLE IF NOT EXISTS`는 이미 만들어진 기존 DB 파일에
            # 새 컬럼을 추가하지 않는다 — 2026-08-15 `source_audio_uri` 추가 전
            # 파일과의 호환을 위해 있으면 조용히 건너뛴다.
            try:
                conn.execute("ALTER TABLE meetings ADD COLUMN source_audio_uri TEXT")
            except sqlite3.OperationalError:
                pass
            # `summary`는 회의 재조회(`GET /api/v1/meetings/{id}/analysis`, 17번 갭
            # 문서 #10)용 — 예전엔 `analyze_meeting` 응답에만 있고 어디에도
            # 저장되지 않아, 화면을 벗어나면 LLM을 다시 불러야만 결과를 다시
            # 볼 수 있었다(2026-08-16).
            try:
                conn.execute("ALTER TABLE meetings ADD COLUMN summary TEXT")
            except sqlite3.OperationalError:
                pass
            # Soft Delete — 회의 삭제 기능 추가(2026-08-17, 사용자 요청). Recording·
            # Transcript·Action Item 등 자식 테이블은 그대로 둔다(Hard Delete는
            # R2 원본 음성·Postgres 검색 색인까지 함께 지워야 해 범위가 커진다 —
            # Task의 `deleted_at` Soft Delete와 같은 선택).
            try:
                conn.execute("ALTER TABLE meetings ADD COLUMN deleted_at TEXT")
            except sqlite3.OperationalError:
                pass
            # Action Item 수정(#8)·근거 재조회(#10·#11)에 필요한 필드를 추가한다 —
            # 예전엔 `title`·`evidence_text`만 저장해 담당자·마감일 수정이나
            # 근거 구간(`evidence_span`) 재조회를 지원할 수 없었다(2026-08-16,
            # 17번 갭 문서).
            for column, ddl in (
                ("description", "TEXT"),
                ("due_at", "TEXT"),
                ("assignee_user_id", "TEXT"),
                ("start_ms", "INTEGER"),
                ("end_ms", "INTEGER"),
                ("meeting_chunk_id", "TEXT"),
            ):
                try:
                    conn.execute(f"ALTER TABLE action_items ADD COLUMN {column} {ddl}")
                except sqlite3.OperationalError:
                    pass
            # `action_item_id`는 LLM이 스스로 매기는 값이라(`analyze_transcript`
            # 응답의 한 필드) 회의마다 새로 나는 무작위 값이 아니다 —
            # `temperature=0`으로 비슷한 회의록에 반복 분석을 돌리면 서로 다른
            # 회의에서 같은 값("action-1" 등)을 매길 수 있다. 예전엔
            # `action_item_id` 하나만 PRIMARY KEY라 이 경우 `INSERT OR IGNORE`가
            # 조용히 건너뛰고 뒤이은 SELECT가 아무 것도 못 찾아 `AssertionError`로
            # 500이 났다(2026-08-15 실사용 중 재현). 기존 DB 파일은 SQLite가
            # PRIMARY KEY 변경을 ALTER TABLE로 지원하지 않아 테이블을 다시 만들어
            # 옮긴다 — 로컬 개발 DB라 데이터 유실 위험이 낮다.
            pk_columns = [row["name"] for row in conn.execute("PRAGMA table_info(action_items)").fetchall() if row["pk"] > 0]
            if pk_columns == ["action_item_id"]:
                # 이 리네임-재생성 시점에는 위 ALTER 루프가 이미 실행돼 예전
                # 단일 PK 테이블에도 신규 컬럼(description 등)이 붙어 있다 —
                # `SELECT *`/`INSERT ... SELECT *`가 두 테이블 모두에서
                # 같은 컬럼 집합을 보게 새 테이블 선언도 맞춰 늘린다.
                conn.executescript(
                    """
                    ALTER TABLE action_items RENAME TO action_items_pre_20260815;
                    CREATE TABLE action_items (
                      action_item_id TEXT NOT NULL, meeting_id TEXT NOT NULL, user_id TEXT NOT NULL,
                      title TEXT NOT NULL, evidence_text TEXT NOT NULL, approval_status TEXT NOT NULL DEFAULT 'pending',
                      task_id TEXT, created_at TEXT NOT NULL,
                      description TEXT, due_at TEXT, assignee_user_id TEXT, start_ms INTEGER, end_ms INTEGER, meeting_chunk_id TEXT,
                      PRIMARY KEY (action_item_id, meeting_id),
                      FOREIGN KEY(meeting_id) REFERENCES meetings(meeting_id)
                    );
                    INSERT INTO action_items SELECT * FROM action_items_pre_20260815;
                    DROP TABLE action_items_pre_20260815;
                    """
                )

    @staticmethod
    def _ts(value: datetime | None) -> str | None:
        return value.astimezone(timezone.utc).isoformat() if value else None

    @staticmethod
    def _dt(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    @classmethod
    def _meeting_record(cls, row: sqlite3.Row) -> MeetingRecord:
        keys = row.keys()
        return MeetingRecord(
            row["meeting_id"], row["user_id"], row["title"], cls._dt(row["started_at"]), cls._dt(row["ended_at"]),
            cls._dt(row["created_at"]), row["source_audio_uri"],
            row["summary"] if "summary" in keys else None,
        )

    def create(self, record: MeetingRecord) -> MeetingRecord:
        """회의를 멱등 생성하고 동일 사용자·ID의 기존 레코드를 반환한다."""
        now = record.created_at or datetime.now(timezone.utc)
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT OR IGNORE INTO meetings (meeting_id, user_id, title, started_at, ended_at, created_at, source_audio_uri) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (record.meeting_id, record.user_id, record.title, self._ts(record.started_at), self._ts(record.ended_at), self._ts(now), record.source_audio_uri))
            row = conn.execute("SELECT * FROM meetings WHERE meeting_id=? AND user_id=?", (record.meeting_id, record.user_id)).fetchone()
        assert row is not None
        return self._meeting_record(row)

    def get(self, meeting_id: str, user_id: str, *, include_deleted: bool = False) -> MeetingRecord | None:
        """요청 사용자 소유 회의만 조회한다.

        기본값은 삭제된 회의를 없는 것처럼 숨긴다(`Task`의 `include_deleted`와
        같은 패턴, `app/repositories/tasks.py`). `include_deleted=True`는
        Task의 "회의록에서 보기" 근거 링크(`app/task_api.py`의
        `_meeting_evidence()`)처럼, 회의를 삭제해도 그 회의에서 나온
        Action Item·Task의 출처 추적은 계속 살아 있어야 하는 곳에서만 쓴다
        (2026-08-17, 사용자 요청 — 회의를 삭제한 뒤에도 그 회의에서 만든
        Task의 "회의록에서 보기"가 계속 연결돼야 한다).
        """

        query = "SELECT * FROM meetings WHERE meeting_id=? AND user_id=?"
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        with closing(self._connect()) as conn:
            row = conn.execute(query, (meeting_id, user_id)).fetchone()
        return self._meeting_record(row) if row else None

    def list(self, user_id: str) -> list[MeetingRecord]:
        """요청 사용자의 삭제되지 않은 회의만 결정적인 순서로 반환한다."""
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM meetings WHERE user_id=? AND deleted_at IS NULL ORDER BY created_at DESC, meeting_id", (user_id,)).fetchall()
        return [self._meeting_record(r) for r in rows]

    def delete(self, meeting_id: str, user_id: str) -> bool:
        """회의를 Soft Delete한다 — Recording·Transcript·Action Item은 그대로 둔다.

        Returns:
            삭제 대상이 실제로 있었으면(이미 삭제됐거나 없는 회의가 아니면) `True`.
        """

        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "UPDATE meetings SET deleted_at=? WHERE meeting_id=? AND user_id=? AND deleted_at IS NULL",
                (self._ts(datetime.now(timezone.utc)), meeting_id, user_id),
            )
        return cursor.rowcount > 0

    def set_title(self, meeting_id: str, user_id: str, title: str) -> None:
        """회의 제목을 갱신한다 — 분석 뒤 자동 생성한 제목으로 바꿀 때 쓴다(2026-08-17)."""

        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE meetings SET title=? WHERE meeting_id=? AND user_id=?", (title, meeting_id, user_id))

    def set_source_audio_uri(self, meeting_id: str, user_id: str, object_key: str) -> MeetingRecord | None:
        """원본 음성이 R2에 확정된 뒤 Object Key를 회의에 연결한다."""
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE meetings SET source_audio_uri=? WHERE meeting_id=? AND user_id=?", (object_key, meeting_id, user_id))
            row = conn.execute("SELECT * FROM meetings WHERE meeting_id=? AND user_id=?", (meeting_id, user_id)).fetchone()
        return self._meeting_record(row) if row else None

    def add_recording(self, record: RecordingRecord) -> RecordingRecord:
        """파일 원본 없이 checksum과 메타데이터만 멱등 저장한다."""
        now = record.created_at or datetime.now(timezone.utc)
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT OR IGNORE INTO recordings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (record.recording_id, record.meeting_id, record.user_id, record.filename, record.content_type, record.size_bytes, record.sha256, record.object_key, self._ts(now)))
            row = conn.execute("SELECT * FROM recordings WHERE meeting_id=? AND user_id=? AND sha256=?", (record.meeting_id, record.user_id, record.sha256)).fetchone()
        assert row is not None
        return RecordingRecord(row["recording_id"], row["meeting_id"], row["user_id"], row["filename"], row["content_type"], row["size_bytes"], row["sha256"], row["object_key"], self._dt(row["created_at"]))

    def session(self, meeting_id: str, user_id: str) -> tuple[int, dict[str, str]]:
        """영속 RecordingSession의 마지막 순번과 checksum 목록을 반환한다."""
        import json
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT OR IGNORE INTO recording_sessions(meeting_id,user_id) VALUES (?,?)", (meeting_id, user_id))
            row = conn.execute("SELECT last_chunk_no, chunk_hashes FROM recording_sessions WHERE meeting_id=? AND user_id=?", (meeting_id, user_id)).fetchone()
        assert row is not None
        return row["last_chunk_no"], json.loads(row["chunk_hashes"])

    def save_session(self, meeting_id: str, user_id: str, last_chunk_no: int, hashes: dict[str, str]) -> None:
        """Chunk 수신 상태를 원자적으로 저장한다."""
        import json
        with closing(self._connect()) as conn, conn:
            conn.execute("""INSERT INTO recording_sessions(meeting_id,user_id,last_chunk_no,chunk_hashes) VALUES (?,?,?,?)
                ON CONFLICT(meeting_id,user_id) DO UPDATE SET last_chunk_no=excluded.last_chunk_no, chunk_hashes=excluded.chunk_hashes""", (meeting_id, user_id, last_chunk_no, json.dumps(hashes, sort_keys=True)))

    def save_transcript(self, meeting_id: str, user_id: str, chunk_no: int, text: str, is_final: bool, start_ms: int | None = None, end_ms: int | None = None) -> dict[str, object]:
        """임시·최종 Transcript를 같은 Chunk에 대해 멱등 저장한다."""
        from uuid import uuid4
        now = datetime.now(timezone.utc)
        with closing(self._connect()) as conn, conn:
            conn.execute("""INSERT OR IGNORE INTO transcripts(transcript_id,meeting_id,user_id,chunk_no,text,is_final,start_ms,end_ms,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""", (str(uuid4()), meeting_id, user_id, chunk_no, text, int(is_final), start_ms, end_ms, self._ts(now)))
            row = conn.execute("SELECT * FROM transcripts WHERE meeting_id=? AND user_id=? AND chunk_no=? AND is_final=?", (meeting_id, user_id, chunk_no, int(is_final))).fetchone()
        assert row is not None
        return dict(row)

    def list_transcripts(self, meeting_id: str, user_id: str) -> list[dict[str, object]]:
        """요청 사용자 소유의 Transcript를 Chunk 순서로 반환한다."""
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM transcripts WHERE meeting_id=? AND user_id=? ORDER BY chunk_no, is_final", (meeting_id, user_id)).fetchall()
        return [dict(row) for row in rows]

    def upsert_action(
        self,
        action_item_id: str,
        meeting_id: str,
        user_id: str,
        title: str,
        evidence_text: str,
        *,
        description: str | None = None,
        due_at: str | None = None,
        assignee_user_id: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        meeting_chunk_id: str | None = None,
    ) -> dict[str, object]:
        """Action Item을 사용자·회의 범위에서 멱등 생성한다."""
        now = datetime.now(timezone.utc)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """INSERT OR IGNORE INTO action_items
                    (action_item_id, meeting_id, user_id, title, evidence_text, approval_status, task_id, created_at,
                     description, due_at, assignee_user_id, start_ms, end_ms, meeting_chunk_id)
                   VALUES (?,?,?,?,?,'pending',NULL,?,?,?,?,?,?,?)""",
                (action_item_id, meeting_id, user_id, title, evidence_text, self._ts(now),
                 description, due_at, assignee_user_id, start_ms, end_ms, meeting_chunk_id),
            )
            row = conn.execute("SELECT * FROM action_items WHERE action_item_id=? AND meeting_id=? AND user_id=?", (action_item_id, meeting_id, user_id)).fetchone()
        assert row is not None
        return dict(row)

    def approve_action(self, action_item_id: str, meeting_id: str, user_id: str, task_id: str) -> dict[str, object] | None:
        """Action Item 승인과 연결 Task ID를 한 번만 기록한다."""
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT * FROM action_items WHERE action_item_id=? AND meeting_id=? AND user_id=?", (action_item_id, meeting_id, user_id)).fetchone()
            if row is None:
                return None
            if row["task_id"] is None:
                conn.execute("UPDATE action_items SET approval_status='approved', task_id=? WHERE action_item_id=? AND meeting_id=? AND user_id=?", (task_id, action_item_id, meeting_id, user_id))
            row = conn.execute("SELECT * FROM action_items WHERE action_item_id=? AND meeting_id=? AND user_id=?", (action_item_id, meeting_id, user_id)).fetchone()
        return dict(row) if row else None

    def reject_action(self, action_item_id: str, meeting_id: str, user_id: str) -> dict[str, object] | None:
        """검토 대기 중인 Action Item을 거절 상태로 남긴다(Task는 만들지 않음, 2026-08-16, 17번 갭 문서 #8)."""
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT * FROM action_items WHERE action_item_id=? AND meeting_id=? AND user_id=?", (action_item_id, meeting_id, user_id)).fetchone()
            if row is None:
                return None
            if row["task_id"] is None:
                conn.execute("UPDATE action_items SET approval_status='rejected' WHERE action_item_id=? AND meeting_id=? AND user_id=?", (action_item_id, meeting_id, user_id))
            row = conn.execute("SELECT * FROM action_items WHERE action_item_id=? AND meeting_id=? AND user_id=?", (action_item_id, meeting_id, user_id)).fetchone()
        return dict(row) if row else None

    def edit_action(
        self,
        action_item_id: str,
        meeting_id: str,
        user_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        due_at: str | None = None,
        assignee_user_id: str | None = None,
    ) -> dict[str, object] | None:
        """Action Item의 담당자·마감일 등을 수정한다 — `evidence_text`는 서버가 원문에서
        추출한 값이라 읽기 전용이고 여기서 바꿀 수 없다(02 §5.3, 2026-08-16, 17번 갭 문서 #8)."""
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT * FROM action_items WHERE action_item_id=? AND meeting_id=? AND user_id=?", (action_item_id, meeting_id, user_id)).fetchone()
            if row is None:
                return None
            conn.execute(
                """UPDATE action_items SET
                    title=COALESCE(?, title), description=COALESCE(?, description),
                    due_at=COALESCE(?, due_at), assignee_user_id=COALESCE(?, assignee_user_id)
                   WHERE action_item_id=? AND meeting_id=? AND user_id=?""",
                (title, description, due_at, assignee_user_id, action_item_id, meeting_id, user_id),
            )
            row = conn.execute("SELECT * FROM action_items WHERE action_item_id=? AND meeting_id=? AND user_id=?", (action_item_id, meeting_id, user_id)).fetchone()
        return dict(row)

    def list_actions(self, meeting_id: str, user_id: str) -> list[dict[str, object]]:
        """회의의 Action Item 후보를 결정적인 순서로 반환한다(2026-08-16, 17번 갭 문서 #10)."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM action_items WHERE meeting_id=? AND user_id=? ORDER BY created_at, action_item_id",
                (meeting_id, user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_summary(self, meeting_id: str, user_id: str, summary: str) -> None:
        """`analyze_meeting`이 만든 요약을 저장해 재조회를 지원한다(2026-08-16, 17번 갭 문서 #10)."""
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE meetings SET summary=? WHERE meeting_id=? AND user_id=?", (summary, meeting_id, user_id))
