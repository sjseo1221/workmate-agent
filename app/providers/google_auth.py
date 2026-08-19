"""Google OAuth2 Credential을 파일에서 읽고 항상 최신 상태로 갱신한다.

`app/dev_gmail_sync_api.py`·`app/dev_calendar_sync_api.py`·
`app/workflows/daily_briefing.py` 세 곳이 각자 이 로직을 거의 그대로 복제해
갖고 있었다(2026-08-17 정리, 캘린더 "Google authorization required" 반복
발생 조사 중 발견). 그중 두 곳은 `credentials.expired`가 `True`일 때만
갱신했는데, 이 세션에서만 그 가정이 두 번 깨졌다 — 저장된 만료 시각이
아직 안 지나 `expired`가 `False`인데도 실제 Access Token은 이미
무효(Calendar Scope 부족 등으로 403)라, 사람이 직접
`credentials.refresh(Request())`를 강제로 호출해야만 매번 고쳐졌다.
google-auth 라이브러리의 `expired`는 저장된 만료 시각만 비교할 뿐 서버에
실제 유효성을 확인하지 않으므로, 이제 Refresh Token이 있으면 `expired`
값과 무관하게 매번 갱신한다. 이 경로들은 사람이 손으로 가끔 호출하는
개발용 동기화 API이거나 몇 분 간격의 백그라운드 Job이라, 매 호출마다
갱신 요청이 하나 늘어도 비용은 무시할 만하다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class GoogleCredentialError(RuntimeError):
    """Google Credential을 읽거나 갱신하지 못했다.

    호출부마다 "Token 파일이 없을 때" 안내 문구·예외 타입(HTTPException 503,
    ProviderConfigurationError 등)이 달라 여기서 통일하지 않는다 — 호출부가
    이 예외를 잡아 자신의 오류 타입으로 바꾼다.
    """


def load_google_credentials(token_path: Path, scopes: list[str]) -> Any:
    """`token_path`의 Credential을 읽고, Refresh Token이 있으면 항상 새로 갱신해 돌려준다.

    `token_path`는 호출부가 이미 존재를 확인했다고 가정한다.
    """

    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2.credentials import Credentials

    credentials = Credentials.from_authorized_user_file(str(token_path), scopes)
    if credentials.refresh_token:
        try:
            credentials.refresh(GoogleAuthRequest())
        except Exception as exc:  # noqa: BLE001 - Google 라이브러리 예외를 통일된 오류로 바꾼다.
            raise GoogleCredentialError(f"Google Token 갱신에 실패했습니다: {exc}") from exc
        token_path.write_text(credentials.to_json(), encoding="utf-8")
    if not credentials.valid:
        raise GoogleCredentialError(f"Google Token이 유효하지 않습니다: {token_path}")
    return credentials


def build_authorized_session(token_path: Path, scopes: list[str]) -> Any:
    """`load_google_credentials`의 결과로 `AuthorizedSession`을 만든다."""

    from google.auth.transport.requests import AuthorizedSession

    return AuthorizedSession(load_google_credentials(token_path, scopes))


def load_google_credentials_from_json(token_json: str, scopes: list[str]) -> Any:
    """저장된 Credential JSON 문자열(파일이 아니라 DB 등)을 읽고 항상 새로 갱신해 돌려준다.

    `load_google_credentials()`와 갱신 로직은 동일하다 — 다만 파일 대신 이미 메모리에
    있는 JSON 문자열(예: `app/repositories/google_credentials.py`에 저장된 사용자별
    Credential)을 다룬다. 갱신된 뒤의 최신 JSON을 저장소에 다시 쓰는 건 호출부의
    책임이다(`credentials.to_json()`) — 이 함수는 어떤 저장소를 쓰는지 모른다.
    """

    import json

    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2.credentials import Credentials

    credentials = Credentials.from_authorized_user_info(json.loads(token_json), scopes)
    if credentials.refresh_token:
        try:
            credentials.refresh(GoogleAuthRequest())
        except Exception as exc:  # noqa: BLE001 - Google 라이브러리 예외를 통일된 오류로 바꾼다.
            raise GoogleCredentialError(f"Google Token 갱신에 실패했습니다: {exc}") from exc
    if not credentials.valid:
        raise GoogleCredentialError("Google Token이 유효하지 않습니다.")
    return credentials


def build_authorized_session_from_json(token_json: str, scopes: list[str]) -> Any:
    """`load_google_credentials_from_json`의 결과로 `AuthorizedSession`을 만든다."""

    from google.auth.transport.requests import AuthorizedSession

    return AuthorizedSession(load_google_credentials_from_json(token_json, scopes))


def build_authorized_session_for_user(user_id: str, scopes: list[str]) -> Any:
    """`user_id`로 저장된 사용자별 Credential을 읽어 세션을 만들고, 갱신되면 다시 저장한다.

    `app/google_oauth_web.py`의 웹 OAuth 흐름으로 연결한 사용자별 Credential
    (`app/repositories/google_credentials.py`)을 쓴다 — 저장된 Credential이 없으면
    `GoogleCredentialError`를 던진다. 호출부가 이걸 "아직 연결 안 함"으로 해석해
    기존 공유 파일 폴백이든 연결 안내든 알아서 처리한다.
    """

    from google.auth.transport.requests import AuthorizedSession

    from app.repositories.google_credentials import google_credential_repository

    repository = google_credential_repository()
    token_json = repository.load(user_id)
    if not token_json:
        raise GoogleCredentialError(f"연결된 Google 계정이 없습니다: {user_id}")
    credentials = load_google_credentials_from_json(token_json, scopes)
    repository.save(user_id, credentials.to_json())
    return AuthorizedSession(credentials)
