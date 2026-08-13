"""내부 검증 챗봇 API의 운영 OIDC 경로 계약 테스트."""

from __future__ import annotations

import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from fastapi.testclient import TestClient

from app.main import app
from app.workflows.registry import WorkflowResult


class _JwksHandler(BaseHTTPRequestHandler):
    """테스트 OIDC issuer가 제공하는 JWKS 응답을 흉내 낸다."""

    jwks: bytes = b"{}"

    def do_GET(self) -> None:  # noqa: N802 - 표준 라이브러리 Handler 계약
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.jwks)))
        self.end_headers()
        self.wfile.write(self.jwks)

    def log_message(self, format: str, *args: object) -> None:
        """테스트 서버의 요청 로그를 출력하지 않는다."""


class InternalChatInputTests(unittest.TestCase):
    """운영과 동일한 OIDC JWT·JWKS 검증 경계를 검증한다."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key = cls.private_key.public_key()
        jwk = json.loads(RSAAlgorithm.to_jwk(public_key))
        jwk["kid"] = "workmate-test-key"
        _JwksHandler.jwks = json.dumps({"keys": [jwk]}).encode("utf-8")
        cls.jwks_server = ThreadingHTTPServer(("127.0.0.1", 0), _JwksHandler)
        cls.jwks_thread = threading.Thread(target=cls.jwks_server.serve_forever, daemon=True)
        cls.jwks_thread.start()
        cls.jwks_url = f"http://127.0.0.1:{cls.jwks_server.server_port}/jwks.json"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.jwks_server.shutdown()
        cls.jwks_server.server_close()
        cls.jwks_thread.join(timeout=5)

    def setUp(self) -> None:
        os.environ["WORKMATE_INTERNAL_CHAT_ENABLED"] = "true"
        os.environ["WORKMATE_OIDC_ISSUER"] = "https://issuer.test"
        os.environ["WORKMATE_OIDC_AUDIENCE"] = "workmate-internal"
        os.environ["WORKMATE_OIDC_JWKS_URL"] = self.jwks_url
        self.client.headers.update({"Authorization": f"Bearer {self._token()}"})

    def tearDown(self) -> None:
        for name in (
            "WORKMATE_INTERNAL_CHAT_ENABLED",
            "WORKMATE_OIDC_ISSUER",
            "WORKMATE_OIDC_AUDIENCE",
            "WORKMATE_OIDC_JWKS_URL",
        ):
            os.environ.pop(name, None)
        self.client.headers.pop("Authorization", None)

    def _token(
        self,
        *,
        subject: str = "contract-user",
        issuer: str = "https://issuer.test",
        audience: str = "workmate-internal",
        expires_at: int | None = None,
    ) -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "sub": subject,
                "iss": issuer,
                "aud": audience,
                "iat": now,
                "exp": expires_at if expires_at is not None else now + 300,
            },
            self.private_key,
            algorithm="RS256",
            headers={"kid": "workmate-test-key"},
        )

    def test_lists_the_same_five_skills_as_agent_card(self) -> None:
        response = self.client.get("/api/v1/internal/skill-chat/skills")
        self.assertEqual(response.status_code, 200)
        card = self.client.get("/.well-known/agent-card.json")
        self.assertEqual(card.status_code, 200)
        self.assertEqual(
            [skill["id"] for skill in response.json()],
            [skill["id"] for skill in card.json()["skills"]],
        )
        self.assertEqual(len(response.json()), 5)

    def test_accepts_natural_language_and_json_input(self) -> None:
        natural = self.client.post(
            "/api/v1/internal/skill-chat/messages",
            json={"skill_id": "weekly_report", "input": "지난주 보고서 요약"},
        )
        self.assertEqual(natural.status_code, 200)
        self.assertEqual(natural.json()["input_type"], "natural_language")
        self.assertEqual(natural.json()["state"], "completed")
        self.assertTrue(natural.json()["artifact"]["mock"])
        self.assertFalse(natural.json()["artifact"]["business_result"])
        task_id = natural.json()["task_id"]
        task = self.client.get(f"/api/v1/internal/skill-chat/tasks/{task_id}")
        self.assertEqual(task.status_code, 200)
        self.assertEqual(task.json()["status"]["state"], "TASK_STATE_COMPLETED")
        cancel = self.client.post(f"/api/v1/internal/skill-chat/tasks/{task_id}:cancel")
        self.assertEqual(cancel.status_code, 409)

        structured = self.client.post(
            "/api/v1/internal/skill-chat/messages",
            json={
                "skill_id": "search_meetings",
                "input": {
                    "schema_version": "1.0",
                    "skill_id": "search_meetings",
                    "user_id": "contract-user",
                    "timezone": "Asia/Seoul",
                    "query": "결정사항",
                },
            },
        )
        self.assertEqual(structured.status_code, 200)
        self.assertEqual(structured.json()["input_type"], "json")

    def test_rejects_unknown_skill_blank_input_and_mismatched_json_skill(self) -> None:
        self.assertEqual(
            self.client.post(
                "/api/v1/internal/skill-chat/messages",
                json={"skill_id": "unknown", "input": "hello"},
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/internal/skill-chat/messages",
                json={"skill_id": "weekly_report", "input": "   "},
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/internal/skill-chat/messages",
                json={"skill_id": "weekly_report", "input": {"skill_id": "daily_briefing"}},
            ).status_code,
            422,
        )

    def test_rejects_user_scope_mismatch(self) -> None:
        response = self.client.post(
            "/api/v1/internal/skill-chat/messages",
            json={
                "skill_id": "search_meetings",
                "input": {
                    "schema_version": "1.0",
                    "skill_id": "search_meetings",
                    "user_id": "another-user",
                    "timezone": "Asia/Seoul",
                    "query": "결정사항",
                },
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_rejects_missing_expired_and_wrong_audience_tokens(self) -> None:
        self.client.headers.pop("Authorization")
        self.assertEqual(
            self.client.get("/api/v1/internal/skill-chat/skills").status_code,
            401,
        )
        self.client.headers.update({"Authorization": f"Bearer {self._token(expires_at=int(time.time()) - 1)}"})
        self.assertEqual(
            self.client.get("/api/v1/internal/skill-chat/skills").status_code,
            401,
        )
        self.client.headers.update({"Authorization": f"Bearer {self._token(audience='wrong-audience')}"})
        self.assertEqual(
            self.client.get("/api/v1/internal/skill-chat/skills").status_code,
            401,
        )

    def test_task_snapshot_is_scoped_to_authenticated_user(self) -> None:
        created = self.client.post(
            "/api/v1/internal/skill-chat/messages",
            json={"skill_id": "weekly_report", "input": "내 보고서"},
        )
        self.assertEqual(created.status_code, 200)
        task_id = created.json()["task_id"]
        self.client.headers.update({"Authorization": f"Bearer {self._token(subject='another-user')}"})
        self.assertEqual(
            self.client.get(f"/api/v1/internal/skill-chat/tasks/{task_id}").status_code,
            404,
        )

    def test_preserves_partial_failure_warnings(self) -> None:
        result = WorkflowResult(
            artifact_name="partial",
            artifact_description="partial result",
            text="사용 가능한 결과",
            warnings=[
                {
                    "source": "gmail",
                    "code": "provider_timeout",
                    "message": "Gmail 응답 지연",
                    "retryable": True,
                }
            ],
        )
        with patch("app.internal_chat.workflow_registry") as registry:
            registry.return_value.execute = AsyncMock(return_value=result)
            response = self.client.post(
                "/api/v1/internal/skill-chat/messages",
                json={"skill_id": "daily_briefing", "input": "오늘 브리핑"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["warnings"], result.warnings)

    def test_disabled_api_and_unknown_task(self) -> None:
        os.environ.pop("WORKMATE_INTERNAL_CHAT_ENABLED", None)
        self.assertEqual(self.client.get("/api/v1/internal/skill-chat/skills").status_code, 404)
        os.environ["WORKMATE_INTERNAL_CHAT_ENABLED"] = "true"
        self.assertEqual(
            self.client.get("/api/v1/internal/skill-chat/tasks/missing").status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
