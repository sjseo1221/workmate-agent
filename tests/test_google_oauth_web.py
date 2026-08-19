"""사용자가 직접 Google 계정을 연결하는 웹 OAuth 흐름(`app/google_oauth_web.py`) 계약 테스트.

실제 Google을 호출하지 않는다 — `_build_flow`만 Fake로 바꿔 `state` 서명·검증,
Credential 저장, 응답 형태를 검증한다.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.google_oauth_web import STATE_SECRET_ENV, _sign_state, _verify_state
from app.internal_chat import _authenticated_user_or_assignee
from app.main import app
from app.repositories.google_credentials import google_credential_repository


class FakeCredentials:
    def __init__(self, payload: str = '{"token": "fake-access-token", "refresh_token": "fake-refresh"}') -> None:
        self._payload = payload

    def to_json(self) -> str:
        return self._payload


class FakeFlow:
    """`Flow.from_client_secrets_file(...)`가 반환하는 객체를 흉내낸다."""

    def __init__(self, authorization_url: str = "https://accounts.google.com/o/oauth2/auth?fake=1") -> None:
        self._authorization_url = authorization_url
        self.credentials = FakeCredentials()
        self.fetch_token_calls: list[str] = []

    def authorization_url(self, **kwargs):
        return self._authorization_url, "unused-google-state"

    def fetch_token(self, code: str) -> None:
        self.fetch_token_calls.append(code)


class StateSigningTests(unittest.TestCase):
    """CSRF 방지용 `state` 서명·검증을 검증한다."""

    def setUp(self) -> None:
        os.environ[STATE_SECRET_ENV] = "test-state-secret"
        self.addCleanup(os.environ.pop, STATE_SECRET_ENV, None)

    def test_sign_then_verify_round_trips_to_the_same_user_id_and_code_verifier(self) -> None:
        state = _sign_state("user-a", "verifier-1")
        self.assertEqual(_verify_state(state), ("user-a", "verifier-1"))

    def test_tampered_payload_is_rejected(self) -> None:
        state = _sign_state("user-a", "verifier-1")
        payload_b64, signature = state.split(".", 1)
        tampered = f"{payload_b64}x.{signature}"
        with self.assertRaises(Exception):
            _verify_state(tampered)

    def test_expired_state_is_rejected(self) -> None:
        with patch("app.google_oauth_web.time.time", return_value=time.time() - 10_000):
            state = _sign_state("user-a", "verifier-1")
        with self.assertRaises(Exception):
            _verify_state(state)

    def test_state_signed_for_one_user_cannot_be_reused_for_another(self) -> None:
        state = _sign_state("user-a", "verifier-1")
        user_id, _ = _verify_state(state)
        self.assertEqual(user_id, "user-a")
        self.assertNotEqual(user_id, "user-b")


class GoogleOAuthWebRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        os.environ[STATE_SECRET_ENV] = "test-state-secret"
        os.environ["WORKMATE_GOOGLE_CREDENTIAL_DB_PATH"] = str(Path(cls.temp_dir.name) / "google_credentials.sqlite3")
        google_credential_repository.cache_clear()
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()
        os.environ.pop(STATE_SECRET_ENV, None)
        os.environ.pop("WORKMATE_GOOGLE_CREDENTIAL_DB_PATH", None)
        google_credential_repository.cache_clear()

    def setUp(self) -> None:
        app.dependency_overrides[_authenticated_user_or_assignee] = lambda: "user-a"
        self.addCleanup(app.dependency_overrides.pop, _authenticated_user_or_assignee, None)
        self.addCleanup(google_credential_repository().delete, "user-a")

    def test_status_reports_not_connected_by_default(self) -> None:
        response = self.client.get("/api/v1/auth/google/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"connected": False})

    def test_start_returns_an_authorization_url_signed_for_the_caller(self) -> None:
        with patch("app.google_oauth_web._build_flow", return_value=FakeFlow()) as fake_build:
            response = self.client.get("/api/v1/auth/google/start")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["authorization_url"], "https://accounts.google.com/o/oauth2/auth?fake=1")
        fake_build.assert_called_once()

    def test_callback_saves_the_credential_and_returns_a_self_closing_page(self) -> None:
        """팝업으로 열리므로 메인 앱으로 리디렉션하지 않는다(2026-08-18) — 대신 팝업
        자신을 닫는 HTML을 돌려주고, 메인 창이 `popup.closed`를 감지해 상태를 다시 묻는다."""

        state = _sign_state("user-a", "verifier-1")
        with patch("app.google_oauth_web._build_flow", return_value=FakeFlow()):
            response = self.client.get(
                "/api/v1/auth/google/callback",
                params={"code": "fake-code", "state": state},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("window.close()", response.text)
        self.assertEqual(
            google_credential_repository().load("user-a"),
            '{"token": "fake-access-token", "refresh_token": "fake-refresh"}',
        )

    def test_callback_rejects_a_tampered_state(self) -> None:
        state = _sign_state("user-a", "verifier-1")
        tampered = state + "x"
        with patch("app.google_oauth_web._build_flow", return_value=FakeFlow()):
            response = self.client.get(
                "/api/v1/auth/google/callback",
                params={"code": "fake-code", "state": tampered},
            )
        self.assertEqual(response.status_code, 400)

    def test_status_reports_connected_after_a_successful_callback(self) -> None:
        state = _sign_state("user-a", "verifier-1")
        with patch("app.google_oauth_web._build_flow", return_value=FakeFlow()):
            self.client.get(
                "/api/v1/auth/google/callback",
                params={"code": "fake-code", "state": state},
                follow_redirects=False,
            )
        response = self.client.get("/api/v1/auth/google/status")
        self.assertEqual(response.json(), {"connected": True})

    def test_disconnect_removes_the_saved_credential(self) -> None:
        google_credential_repository().save("user-a", '{"token": "x"}')
        response = self.client.post("/api/v1/auth/google/disconnect")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(google_credential_repository().load("user-a"))

    def test_start_requires_authentication(self) -> None:
        app.dependency_overrides.pop(_authenticated_user_or_assignee, None)
        try:
            response = self.client.get("/api/v1/auth/google/start")
        finally:
            app.dependency_overrides[_authenticated_user_or_assignee] = lambda: "user-a"
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
