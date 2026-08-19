-- M1.1-01: Task·외부 소스 동기화 업무 도메인 Migration.
-- A2A Task·Message·Artifact·Checkpoint 인프라는 001에서 별도로 소유한다.

-- user_id는 uuid가 아니라 text다 — 실제 값은 내부에서 발급하는 게 아니라 Google OIDC의
-- `sub` Claim을 그대로 쓰는데, 이 값은 큰 10진수 문자열이라 uuid 형식이 아니다(예:
-- "104645315427066509691"). 그동안 SQLite 저장소(`app/repositories/tasks.py` 등)는 처음부터
-- `user_id TEXT`로 맞게 만들어져 있었는데, 이 Postgres Migration만 uuid로 잘못 선언돼 있었다 —
-- `search_meetings`가 실제 Postgres에 쓰는 이 세션 전까지 실사용 경로가 한 번도 없어 발견되지
-- 않았다(2026-08-16, 14번 갭 문서 참고).
CREATE TABLE IF NOT EXISTS users (
    user_id text PRIMARY KEY,
    display_name text NOT NULL,
    timezone text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- task_id도 uuid가 아니라 text다 — 제안함에서 승인된 Task는 `app/proposal_api.py`가
-- `f"proposal-{source_type}-{source_id}"` 형태의 결정적 문자열 ID를 쓴다(같은 제안을 다시
-- 승인해도 같은 Task가 되도록). 수동으로 등록한 Task만 `uuid4()`를 쓴다. SQLite 스키마는
-- 처음부터 `task_id TEXT`였다(2026-08-16, 14번 갭 문서 — 실사용자 Task를 Postgres로
-- 이전하다가 재현).
CREATE TABLE IF NOT EXISTS tasks (
    task_id text PRIMARY KEY,
    assignee_user_id text NOT NULL REFERENCES users(user_id),
    title text NOT NULL,
    status text NOT NULL CHECK (status IN ('todo', 'in_progress', 'blocked', 'done', 'cancelled')),
    priority_hint integer CHECK (priority_hint IS NULL OR priority_hint BETWEEN 0 AND 10),
    due_at timestamptz,
    source_type text NOT NULL CHECK (source_type IN ('manual', 'action_item', 'email', 'calendar')),
    source_id text,
    deleted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((source_type = 'manual' AND source_id IS NULL) OR (source_type <> 'manual' AND source_id IS NOT NULL))
);

-- 삭제된 행도 원본 재수집으로 중복 Task가 생성되지 않도록 원본 키를 계속 보존한다.
CREATE UNIQUE INDEX IF NOT EXISTS tasks_source_unique_idx
    ON tasks (assignee_user_id, source_type, source_id)
    WHERE source_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS tasks_assignee_active_idx
    ON tasks (assignee_user_id, updated_at DESC)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS tasks_due_active_idx
    ON tasks (assignee_user_id, due_at)
    WHERE deleted_at IS NULL AND status NOT IN ('done', 'cancelled');

CREATE TABLE IF NOT EXISTS source_sync_states (
    source_sync_state_id uuid PRIMARY KEY,
    sync_user_id text NOT NULL REFERENCES users(user_id),
    source_type text NOT NULL CHECK (source_type IN ('gmail', 'google_calendar')),
    sync_cursor text,
    channel_id text,
    -- channel_token에는 Calendar Webhook의 암호화된 값만 저장한다.
    channel_token text,
    resource_id text,
    watch_expiration timestamptz,
    last_synced_at timestamptz,
    status text NOT NULL CHECK (status IN ('idle', 'running', 'succeeded', 'failed')),
    error_message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (sync_user_id, source_type)
);

CREATE INDEX IF NOT EXISTS source_sync_states_expiration_idx
    ON source_sync_states (watch_expiration);
