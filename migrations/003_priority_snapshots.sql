-- M1.5-02: 결정적 우선순위 계산 결과와 직전 비교용 Snapshot.

CREATE TABLE IF NOT EXISTS priority_snapshots (
    priority_snapshot_id uuid PRIMARY KEY,
    -- user_id는 uuid가 아니라 text다(002_work_domain.sql 주석 참고 — Google OIDC `sub` 그대로 씀).
    recipient_user_id text NOT NULL REFERENCES users(user_id),
    -- task_id는 uuid가 아니라 text다(002_work_domain.sql 주석 참고 — 제안함 승인 Task는
    -- 결정적 문자열 ID를 씀).
    ranked_task_id text NOT NULL REFERENCES tasks(task_id),
    calculated_at timestamptz NOT NULL,
    as_of timestamptz NOT NULL,
    rank integer NOT NULL CHECK (rank > 0),
    score integer NOT NULL CHECK (score BETWEEN 0 AND 95),
    score_breakdown jsonb NOT NULL,
    reasons jsonb NOT NULL,
    UNIQUE (recipient_user_id, as_of, ranked_task_id)
);

CREATE INDEX IF NOT EXISTS priority_snapshots_latest_idx
    ON priority_snapshots (recipient_user_id, calculated_at DESC, rank ASC);
