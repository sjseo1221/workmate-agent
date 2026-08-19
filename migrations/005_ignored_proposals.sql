-- M-2026-08-16: 제안함에서 "무시"한 Gmail·Calendar 후보를 이후 동기화에서
-- 영구 제외하기 위한 최소 저장. "제안은 DB에 저장하지 않는다"(04
-- §7)는 제안 본문·제목·LLM 근거를 저장하지 않는다는 원칙이고, 이 테이블은
-- 그런 내용을 전혀 담지 않는다 — 사용자가 이미 봤다는 사실(ID·시각)만
-- 남긴다. user_id는 uuid가 아니라 text다(002_work_domain.sql 주석 참고 —
-- Google OIDC `sub`를 그대로 씀).

CREATE TABLE IF NOT EXISTS ignored_proposals (
    user_id text NOT NULL REFERENCES users(user_id),
    source_type text NOT NULL CHECK (source_type IN ('email', 'calendar')),
    source_id text NOT NULL,
    ignored_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, source_type, source_id)
);
