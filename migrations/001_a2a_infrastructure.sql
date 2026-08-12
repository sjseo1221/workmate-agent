-- M0.1-03: 업무 도메인과 분리된 A2A Task·Workflow 인프라.
-- 공개 이벤트 전체 이력은 저장하지 않으며 Task Snapshot과 최소 Artifact만 보존한다.

CREATE TABLE IF NOT EXISTS a2a_tasks (
    task_id text PRIMARY KEY,
    context_id text NOT NULL,
    owner text NOT NULL,
    state text NOT NULL,
    task_json jsonb NOT NULL,
    thread_id text NOT NULL UNIQUE,
    idempotency_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz
);

CREATE TABLE IF NOT EXISTS a2a_messages (
    message_id text PRIMARY KEY,
    task_id text NOT NULL REFERENCES a2a_tasks(task_id) ON DELETE CASCADE,
    request_hash text NOT NULL,
    response_json jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz
);

CREATE TABLE IF NOT EXISTS a2a_artifacts (
    artifact_id text PRIMARY KEY,
    task_id text NOT NULL REFERENCES a2a_tasks(task_id) ON DELETE CASCADE,
    artifact_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz
);

CREATE TABLE IF NOT EXISTS a2a_checkpoints (
    thread_id text NOT NULL,
    checkpoint_id text NOT NULL,
    checkpoint_json jsonb NOT NULL,
    metadata_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, checkpoint_id)
);

CREATE INDEX IF NOT EXISTS a2a_tasks_state_idx ON a2a_tasks (state);
CREATE INDEX IF NOT EXISTS a2a_tasks_updated_idx ON a2a_tasks (updated_at DESC);
CREATE INDEX IF NOT EXISTS a2a_messages_expires_idx ON a2a_messages (expires_at);
CREATE INDEX IF NOT EXISTS a2a_artifacts_expires_idx ON a2a_artifacts (expires_at);
