-- M4.1-01: 회의 검색 Chunk와 Embedding 저장 계약.
-- PostgreSQL을 단일 저장소로 사용하며 Vector DB는 별도로 도입하지 않는다.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- user_id는 uuid가 아니라 text다(002_work_domain.sql 주석 참고 — Google OIDC `sub` 그대로 씀).
CREATE TABLE IF NOT EXISTS meetings (
    meeting_id uuid PRIMARY KEY,
    user_id text NOT NULL REFERENCES users(user_id),
    title text NOT NULL,
    started_at timestamptz,
    ended_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (meeting_id, user_id)
);

CREATE TABLE IF NOT EXISTS meeting_chunks (
    meeting_chunk_id uuid PRIMARY KEY,
    parent_meeting_id uuid NOT NULL,
    user_id text NOT NULL REFERENCES users(user_id),
    sequence_no integer NOT NULL CHECK (sequence_no >= 0),
    speaker text,
    started_at_ms integer,
    ended_at_ms integer,
    content text NOT NULL,
    embedding vector(1536) NOT NULL,
    embedding_model text NOT NULL DEFAULT 'openai/text-embedding-3-small',
    embedding_version text NOT NULL DEFAULT '1',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (parent_meeting_id, user_id, sequence_no),
    FOREIGN KEY (parent_meeting_id, user_id)
        REFERENCES meetings(meeting_id, user_id)
        ON DELETE CASCADE,
    CHECK (started_at_ms IS NULL OR started_at_ms >= 0),
    CHECK (ended_at_ms IS NULL OR ended_at_ms >= 0),
    CHECK (
        started_at_ms IS NULL
        OR ended_at_ms IS NULL
        OR ended_at_ms >= started_at_ms
    )
);

CREATE INDEX IF NOT EXISTS meeting_chunks_meeting_idx
    ON meeting_chunks (parent_meeting_id, user_id, sequence_no);

CREATE INDEX IF NOT EXISTS meeting_chunks_content_trgm_idx
    ON meeting_chunks USING gin (content gin_trgm_ops);

CREATE INDEX IF NOT EXISTS meeting_chunks_embedding_idx
    ON meeting_chunks USING hnsw (embedding vector_cosine_ops);
