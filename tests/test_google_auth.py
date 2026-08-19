"""`app/providers/google_auth.py`의 Credential 로드·강제 갱신 동작 검증.

`app/dev_gmail_sync_api.py`·`app/dev_calendar_sync_api.py`·
`app/workflows/daily_briefing.py`가 예전에 각자 갖고 있던 로직을
2026-08-17에 이 모듈로 통합했다 — "저장된 만료 시각이 아직 안 지났으면
갱신을 건너뛴다"는 예전 방식이 Calendar 403("Google authorization
required")을 이 세션에서만 두 번 유발했다. 이제 Refresh Token이 있으면
`credentials.expired` 값과 무관하게 매번 갱신한다.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.providers.google_auth import GoogleCredentialError, build_authorized_session, load_google_credentials

_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]


class LoadGoogleCredentialsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.token_path = Path(self.directory.name) / "token.json"

    def _write_token(self, *, expired: bool, token: str = "old-access-token") -> None:
        expiry = "2000-01-01T00:00:00Z" if expired else "2999-01-01T00:00:00Z"
        self.token_path.write_text(
            json.dumps(
                {
                    "token": token,
                    "refresh_token": "refresh-token-value",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "client_id": "client-id-value",
                    "client_secret": "client-secret-value",
                    "scopes": _SCOPES,
                    "expiry": expiry,
                }
            ),
            encoding="utf-8",
        )

    def test_not_yet_expired_token_is_still_force_refreshed(self) -> None:
        """`expired=False`(저장된 만료 시각이 안 지남)여도 항상 갱신한다 — 핵심 회귀 방지 테스트."""

        self._write_token(expired=False)

        def _fake_refresh(self, request):  # noqa: ANN001 - google-auth 시그니처를 그대로 흉내낸다.
            self.token = "refreshed-access-token"
            self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

        with patch("google.oauth2.credentials.Credentials.refresh", _fake_refresh):
            credentials = load_google_credentials(self.token_path, _SCOPES)

        self.assertEqual(credentials.token, "refreshed-access-token")
        persisted = json.loads(self.token_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["token"], "refreshed-access-token")

    def test_expired_token_is_refreshed_and_persisted(self) -> None:
        self._write_token(expired=True)

        def _fake_refresh(self, request):  # noqa: ANN001
            self.token = "refreshed-access-token"
            self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

        with patch("google.oauth2.credentials.Credentials.refresh", _fake_refresh):
            credentials = load_google_credentials(self.token_path, _SCOPES)

        self.assertEqual(credentials.token, "refreshed-access-token")

    def test_refresh_failure_raises_google_credential_error(self) -> None:
        self._write_token(expired=False)

        def _raise(self, request):  # noqa: ANN001
            raise RuntimeError("network unreachable")

        with patch("google.oauth2.credentials.Credentials.refresh", _raise):
            with self.assertRaises(GoogleCredentialError) as ctx:
                load_google_credentials(self.token_path, _SCOPES)
        self.assertIn("network unreachable", str(ctx.exception))

    def test_no_refresh_token_skips_refresh_and_uses_stored_access_token(self) -> None:
        """Refresh Token 자체가 없으면(=아직 최초 동의를 안 함) 갱신을 시도하지 않는다."""

        self.token_path.write_text(
            json.dumps(
                {
                    "token": "bare-access-token",
                    # google-auth는 `refresh_token` 키 자체는 요구하지만(없으면
                    # `from_authorized_user_file`이 즉시 ValueError를 던진다)
                    # 값은 `None`이어도 된다 — Refresh Token이 아직 없는 상태를
                    # 이렇게 표현한다.
                    "refresh_token": None,
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "client_id": "client-id-value",
                    "client_secret": "client-secret-value",
                    "scopes": _SCOPES,
                    "expiry": "2999-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        with patch("google.oauth2.credentials.Credentials.refresh") as refresh:
            credentials = load_google_credentials(self.token_path, _SCOPES)
        refresh.assert_not_called()
        self.assertEqual(credentials.token, "bare-access-token")

    def test_build_authorized_session_wraps_the_refreshed_credentials(self) -> None:
        self._write_token(expired=False, token="old-access-token")

        def _fake_refresh(self, request):  # noqa: ANN001
            self.token = "refreshed-access-token"
            self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

        with patch("google.oauth2.credentials.Credentials.refresh", _fake_refresh):
            session = build_authorized_session(self.token_path, _SCOPES)
        self.assertEqual(session.credentials.token, "refreshed-access-token")


if __name__ == "__main__":
    unittest.main()
