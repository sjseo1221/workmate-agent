""""제안함" 화면에서 사용자가 직접 자기 Google 계정을 연결하는 웹 OAuth 흐름.

지금까지 Gmail·Calendar 동기화는 `google-oauth-test/token.json` 파일 하나(고정 데모
계정)만 모든 사용자가 공유했다(15번 문서 "📬 제안함" 절, 2026-08-18 "다른 담당자가
실행할 때..." 논의). 이 모듈은 "Google 계정 연결" 버튼을 누르면 실제 Google 로그인
화면으로 이동해 자기 계정으로 동의하고, 그 Credential이 자기 user_id 몫으로 저장되게
한다(`app/repositories/google_credentials.py`). 기존 공유 파일 방식은 그대로 남겨둔다
— 아직 개별 연결을 안 한 사용자는 계속 공유 계정으로 동작한다(원칙: 기존 기능
비영향).

`google-oauth-test/client_secret.json`은 "installed"(Desktop App) 타입 OAuth
Client다(실측 확인, `redirect_uris`가 `["http://localhost"]`) — Google은 이 타입에
한해 `http://localhost`(포트 무관)로의 Loopback 리디렉션을 사전 등록 없이 허용하므로,
이 웹 흐름에도 Google Cloud Console 설정을 새로 바꾸지 않고 그대로 재사용할 수 있다.

CSRF 방지: `/start`가 발급하는 `state`는 `hmac`으로 서명한 `user_id`+만료시각이다 —
`WORKMATE_OAUTH_STATE_SECRET`(없으면 `WORKMATE_SERVICE_TOKEN`)을 서명 키로 쓴다. 이
흐름 하나를 위해 새 필수 환경변수를 추가하지 않기 위해서다.

PKCE `code_verifier`도 이 `state`에 함께 실어 왕복시킨다 — `google-auth-oauthlib`의
`Flow`는 PKCE를 기본으로 켜고(`autogenerate_code_verifier=True`) `code_verifier`를
그 `Flow` 인스턴스에만 저장하는데, `/start`와 `/callback`은 서로 다른 요청(따라서 서로
다른 `Flow` 인스턴스)이라 그대로 두면 `/callback`이 `code_verifier`를 몰라 Google이
`invalid_grant: Missing code verifier`로 거부한다(2026-08-18, 실사용 중 발견). `state`가
이미 서명·검증되는 왕복 값이라 별도 서버 저장소 없이 여기 실어 보내는 게 제일 간단하다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path
from random import SystemRandom
from string import ascii_letters, digits
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, Response

from app.internal_chat import _authenticated_user_or_assignee
from app.repositories.google_credentials import google_credential_repository

logger = logging.getLogger("workmate-agent.google-oauth-web")

# Google이 동의 화면에서 `openid`/`userinfo.email`/`userinfo.profile`을 요청한 적 없어도
# 자동으로 얹어 돌려줄 때가 있다(계정에 이미 다른 앱으로 동의된 이력이 있으면 특히 그렇다,
# 2026-08-18 실사용 중 발견) — `oauthlib`은 기본적으로 이런 Scope 불일치를 오류로 본다
# (`oauthlib/oauth2/rfc6749/parameters.py::validate_token_parameters`). 우리가 요청한
# `_SCOPES`가 응답에 전부 포함돼 있으면 충분하므로, 이 표준 환경변수로 완화한다.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

router = APIRouter(prefix="/api/v1/auth/google", tags=["google-oauth"])

_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]
CLIENT_SECRET_FILE_ENV = "GOOGLE_CLIENT_SECRET_FILE"
STATE_SECRET_ENV = "WORKMATE_OAUTH_STATE_SECRET"
SERVICE_TOKEN_ENV = "WORKMATE_SERVICE_TOKEN"
REDIRECT_BASE_URL_ENV = "WORKMATE_OAUTH_REDIRECT_BASE_URL"
_DEFAULT_OAUTH_TEST_DIR = Path(__file__).resolve().parents[2] / "google-oauth-test"
_DEFAULT_REDIRECT_BASE_URL = "http://localhost:8001"
_STATE_TTL_SECONDS = 600


def _client_secret_path() -> Path:
    return Path(os.getenv(CLIENT_SECRET_FILE_ENV, str(_DEFAULT_OAUTH_TEST_DIR / "client_secret.json")))


def _redirect_uri() -> str:
    base = os.getenv(REDIRECT_BASE_URL_ENV, _DEFAULT_REDIRECT_BASE_URL).rstrip("/")
    return f"{base}/api/v1/auth/google/callback"


def _state_secret() -> bytes:
    secret = os.getenv(STATE_SECRET_ENV) or os.getenv(SERVICE_TOKEN_ENV) or ""
    if not secret:
        raise HTTPException(
            status_code=503,
            detail=f"{STATE_SECRET_ENV} 또는 {SERVICE_TOKEN_ENV}가 설정되지 않았습니다.",
        )
    return secret.encode("utf-8")


def _generate_code_verifier() -> str:
    """PKCE `code_verifier`를 직접 생성한다(RFC 7636) — `Flow`의 자동 생성 대신 미리
    만들어 `state`에 함께 실을 수 있게 한다(`authorization_url()` 호출 전에 값이
    필요하기 때문)."""

    chars = ascii_letters + digits + "-._~"
    rnd = SystemRandom()
    return "".join(rnd.choice(chars) for _ in range(128))


def _sign_state(user_id: str, code_verifier: str) -> str:
    payload = json.dumps(
        {"user_id": user_id, "code_verifier": code_verifier, "exp": int(time.time()) + _STATE_TTL_SECONDS},
        ensure_ascii=False,
    )
    payload_b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    signature = hmac.new(_state_secret(), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{signature}"


def _verify_state(state: str) -> tuple[str, str]:
    """서명·만료를 확인하고 `state`에 실린 `(user_id, code_verifier)`를 반환한다.

    실패하면 위조·재사용·만료된 `state`라는 뜻이라 400으로 거부한다 —
    이 콜백은 Google이 호출하므로 우리 OIDC Bearer Token 인증을 못 붙이고,
    대신 이 서명이 그 자리를 대신한다.
    """

    try:
        payload_b64, signature = state.split(".", 1)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid state") from None
    expected = hmac.new(_state_secret(), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=400, detail="invalid state signature")
    padding = "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid state payload") from exc
    if float(payload.get("exp", 0)) < time.time():
        raise HTTPException(status_code=400, detail="state expired, please try connecting again")
    user_id = payload.get("user_id")
    code_verifier = payload.get("code_verifier")
    if not isinstance(user_id, str) or not user_id or not isinstance(code_verifier, str) or not code_verifier:
        raise HTTPException(status_code=400, detail="invalid state payload")
    return user_id, code_verifier


def _build_flow(redirect_uri: str, code_verifier: str) -> Any:
    from google_auth_oauthlib.flow import Flow

    secret_path = _client_secret_path()
    if not secret_path.exists():
        raise HTTPException(status_code=503, detail=f"Google Client Secret 파일이 없습니다: {secret_path}")
    return Flow.from_client_secrets_file(
        str(secret_path), scopes=_SCOPES, redirect_uri=redirect_uri, code_verifier=code_verifier
    )


@router.get("/status")
def google_connection_status(user_id: str = Depends(_authenticated_user_or_assignee)) -> dict[str, bool]:
    """이 사용자가 개별 Google 계정을 연결해 뒀는지 알려준다."""

    connected = google_credential_repository().load(user_id) is not None
    return {"connected": connected}


@router.get("/start")
def start_google_connection(user_id: str = Depends(_authenticated_user_or_assignee)) -> dict[str, str]:
    """이 사용자 몫으로 서명한 Google 인증 URL을 만들어 돌려준다.

    프론트가 이 응답의 `authorization_url`로 **팝업 창**을 연다(`window.open()`) — 메인
    탭을 그대로 이동시키면(`window.location.href`) `workmate-ui`가 로그인 토큰을 React
    state로만 들고 있어(어떤 storage에도 저장 안 함) 전체 페이지 이동이 새로고침과 같은
    효과를 내 토큰이 사라진다(2026-08-18 실사용 중 발견). Google로의 이동 자체엔 우리
    Bearer Token이 필요 없어서, 이 조회 단계에서만 인증하고 실제 이동은 별도 단계로 나눴다.
    """

    code_verifier = _generate_code_verifier()
    flow = _build_flow(_redirect_uri(), code_verifier)
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
        state=_sign_state(user_id, code_verifier),
    )
    return {"authorization_url": authorization_url}


@router.get("/callback", response_model=None)
def google_connection_callback(code: str = Query(...), state: str = Query(...)) -> Response:
    """Google이 동의 후 돌려보내는 요청 — `code`를 실제 Credential로 교환해 저장한다.

    프론트는 `window.open()` **팝업**으로 이 흐름 전체(Google 동의 → 이 콜백)를 처리한다
    (`window.location.href` 전체 페이지 이동이었다면, `workmate-ui`가 로그인 토큰을 어떤
    storage에도 저장하지 않고 React state로만 들고 있어(`lib/workmate-auth.tsx`) 이동
    자체가 새로고침과 같은 효과를 내 토큰이 사라졌다 — 2026-08-18 실사용 중 발견). 그래서
    이 응답은 메인 앱으로 리디렉션하지 않고, 팝업 자신을 닫는 페이지만 반환한다 — 메인 창은
    `popup.closed`를 감지해 연결 상태를 다시 조회한다.
    """

    user_id, code_verifier = _verify_state(state)
    flow = _build_flow(_redirect_uri(), code_verifier)
    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # noqa: BLE001 - Google 라이브러리 예외를 하나로 통일한다.
        logger.warning("google_oauth_callback_failed user_id=%s error=%s", user_id, exc)
        return HTMLResponse(
            f"<p>Google 인증에 실패했습니다: {exc}</p><p>이 창을 닫고 다시 시도하세요.</p>",
            status_code=502,
        )
    google_credential_repository().save(user_id, flow.credentials.to_json())
    logger.info("google_oauth_connected user_id=%s", user_id)
    return HTMLResponse(
        "<p>Google 계정이 연결됐습니다. 이 창은 자동으로 닫힙니다...</p><script>window.close();</script>"
    )


@router.post("/disconnect")
def disconnect_google_connection(user_id: str = Depends(_authenticated_user_or_assignee)) -> dict[str, bool]:
    """이 사용자의 저장된 Google 연결을 해제한다(테스트·재연결용)."""

    google_credential_repository().delete(user_id)
    return {"connected": False}


__all__ = [
    "router",
    "CLIENT_SECRET_FILE_ENV",
    "STATE_SECRET_ENV",
    "REDIRECT_BASE_URL_ENV",
]
