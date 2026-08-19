"""FastAPI entrypoint for the Workmate A2A agent."""

from __future__ import annotations

import asyncio
import hmac
import logging
import sys
from uuid import uuid4

import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.a2a.runtime import (
    A2A_VERSION,
    build_runtime_routes,
    is_a2a_path,
    service_token,
)
from app.internal_chat import router as internal_chat_router
from app.meeting_api import router as meeting_router
from app.recording_stream import router as recording_stream_router
from app.transcript_api import router as transcript_router
from app.action_item_api import router as action_item_router
from app.internal_chat_ui import router as internal_chat_ui_router
from app.proposal_api import router as proposal_router
from app.task_api import router as task_router
from app.task_ui import router as task_ui_router
from app.weekly_report_ui import router as weekly_report_ui_router
from app.dev_gmail_sync_api import router as dev_gmail_sync_router
from app.dev_calendar_sync_api import router as dev_calendar_sync_router, webhook_router as calendar_webhook_router
from app.google_oauth_web import router as google_oauth_web_router


# Windows의 기본 ProactorEventLoop는 psycopg async 연결을 지원하지 않는다.
# 개발 환경에서 DATABASE_URL을 사용할 때도 Docker/Linux와 같은 비동기 계약을
# 유지하도록 애플리케이션 시작 전에 Selector 정책을 선택한다.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _force_ipv4_dns() -> None:
    """이 로컬 Windows 환경에서 `oauth2.googleapis.com` 등 Google 호스트에 IPv6로
    연결하면 응답 없이 무한 대기하고 IPv4로는 즉시 성공하는 환경 차이가 있다
    (`07-technical-specification.md` §10이 문서화한 httplib2 IPv6 timeout·IPv4
    fallback 부재 문제와 같은 종류 — `google-auth`의 `requests` 기반 전송에서도
    Token 갱신 단계에서 동일하게 재현됨, 2026-08-15 확인). `socket.getaddrinfo`가
    반환하는 주소 목록에서 IPv6를 필터링해 재현 경로를 원천 차단한다.

    운영 배포(Docker/Linux)는 이 문제가 없다고 확인되면
    `WORKMATE_DISABLE_IPV4_ONLY_DNS=true`로 끌 수 있다.
    """

    if os.getenv("WORKMATE_DISABLE_IPV4_ONLY_DNS", "").lower() == "true":
        return
    import socket

    _original_getaddrinfo = socket.getaddrinfo

    def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002 - socket 표준 시그니처를 그대로 따른다.
        return _original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _ipv4_only_getaddrinfo


_force_ipv4_dns()


STANDALONE_UI_ORIGINS_ENV = "WORKMATE_STANDALONE_UI_ORIGINS"
_DEFAULT_STANDALONE_UI_ORIGINS = (
    "http://localhost:5175,http://127.0.0.1:5175,"
    "http://localhost:3000,http://127.0.0.1:3000,"
    "http://localhost:3001,http://127.0.0.1:3001,"
    # 19번 문서 결정 3 — 오케스트레이터(AI-agent_game_platform) 프론트가 실제로 뜨는 포트.
    "http://localhost:5173,http://127.0.0.1:5173"
)


def _standalone_ui_origins() -> list[str]:
    """`workmate-ui`처럼 별도 Origin에서 API를 호출하는 개발용 UI만 허용한다.

    운영 배포에는 이 CORS 목록에 의존하지 않는다. Orchestrator·adapter는
    Docker 내부 통신이라 브라우저 CORS 대상이 아니며, 이 설정은 로컬에서
    별도 폴더의 정적 UI를 열 때만 필요하다.
    """

    configured = os.getenv(STANDALONE_UI_ORIGINS_ENV, _DEFAULT_STANDALONE_UI_ORIGINS)
    return [origin.strip() for origin in configured.split(",") if origin.strip()]


app = FastAPI(title="Workmate AI Agent", version="0.1.0")
logger = logging.getLogger("workmate-agent")
if not logger.handlers:
    # uvicorn.run()의 기본 logging.config.dictConfig는 "uvicorn"·"uvicorn.error"·
    # "uvicorn.access"만 다루고 root Logger에는 Handler를 붙이지 않는다. 이
    # Logger("workmate-agent"와 그 하위 "workmate-agent.*")는 그 어떤 것에도
    # 걸리지 않아, Handler 없이는 INFO 로그가 어디에도 출력되지 않고 조용히
    # 사라진다. 이 App 안에서만 유효한 최소 Handler를 직접 붙인다.
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_standalone_ui_origins(),
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["authorization", "content-type", "idempotency-key", "x-workmate-assignee"],
)
for route in build_runtime_routes():
    app.router.routes.append(route)
app.include_router(internal_chat_router)
app.include_router(meeting_router)
app.include_router(recording_stream_router)
app.include_router(transcript_router)
app.include_router(action_item_router)
app.include_router(internal_chat_ui_router)
app.include_router(proposal_router)
app.include_router(task_router)
app.include_router(task_ui_router)
app.include_router(weekly_report_ui_router)
app.include_router(dev_gmail_sync_router)
app.include_router(dev_calendar_sync_router)
app.include_router(calendar_webhook_router)
app.include_router(google_oauth_web_router)


@app.middleware("http")
async def verify_a2a_headers(request: Request, call_next):
    """Protect SDK A2A routes with the existing service-token contract.

    Returns:
        The downstream response for valid requests, or a JSON response with
        status 503, 401, or 400 when configuration, authentication, or the
        A2A version header is invalid.

    Contract:
        The Agent Card route remains public. Only the ``/a2a`` route space is
        protected, and the token value is read from ``WORKMATE_SERVICE_TOKEN``.
    """

    request_id = request.headers.get("X-Request-ID") or str(uuid4())
    if is_a2a_path(request.url.path):
        token = service_token()
        if not token:
            return JSONResponse(
                {"error": "Service token is not configured"}, status_code=503
            )
        expected = f"Bearer {token}"
        authorization = request.headers.get("authorization", "")
        if not hmac.compare_digest(authorization, expected):
            return JSONResponse({"error": "Invalid service token"}, status_code=401)
        if request.headers.get("A2A-Version") != A2A_VERSION:
            return JSONResponse(
                {"error": "A2A-Version must be 1.0"}, status_code=400
            )
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    logger.info("request_complete request_id=%s method=%s path=%s status=%s", request_id, request.method, request.url.path, response.status_code)
    if request.url.path == "/.well-known/agent-card.json":
        response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.get("/health/live")
def health_live() -> dict[str, str]:
    """Return a liveness response without requiring provider credentials.

    Returns:
        ``{"status": "alive"}`` when the process can serve requests.
    """

    return {"status": "alive"}


@app.get("/health/ready")
def health_ready() -> dict[str, str]:
    """Return readiness only when the service token is configured.

    Returns:
        ``{"status": "ready"}`` on success; otherwise a 503 response that
        identifies the missing configuration without exposing the token.
    """

    if not service_token():
        return JSONResponse(
            {"status": "not_ready", "reason": "service_token_missing"},
            status_code=503,
        )
    return {"status": "ready"}
