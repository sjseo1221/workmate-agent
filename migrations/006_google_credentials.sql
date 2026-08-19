-- M-2026-08-18: "제안함" 화면에서 각 담당자가 자기 Google 계정을 직접 연결(웹 OAuth
-- 흐름, app/google_oauth_web.py)할 수 있게 사용자별 Credential을 저장한다. 이전까지는
-- google-oauth-test/token.json 파일 하나(고정 데모 계정)만 모든 사용자가 공유했다
-- (15번 문서 "📬 제안함" 절 참고). user_id는 uuid가 아니라 text다(002_work_domain.sql
-- 주석 참고 — Google OIDC `sub`를 그대로 씀). token_json은 refresh_token을 포함한
-- 민감 정보라 애플리케이션 레벨 암호화 없이 평문 저장한다는 점에 주의 — MVP 범위.

CREATE TABLE IF NOT EXISTS google_credentials (
    user_id text PRIMARY KEY REFERENCES users(user_id),
    token_json text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
