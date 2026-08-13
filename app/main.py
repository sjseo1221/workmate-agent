"""FastAPI entrypoint for the Workmate A2A agent."""

from __future__ import annotations

import asyncio
import hmac
import sys

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.a2a.runtime import (
    A2A_VERSION,
    build_runtime_routes,
    is_a2a_path,
    service_token,
)
from app.internal_chat import router as internal_chat_router
from app.internal_chat_ui import router as internal_chat_ui_router


# Windows의 기본 ProactorEventLoop는 psycopg async 연결을 지원하지 않는다.
# 개발 환경에서 DATABASE_URL을 사용할 때도 Docker/Linux와 같은 비동기 계약을
# 유지하도록 애플리케이션 시작 전에 Selector 정책을 선택한다.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


app = FastAPI(title="Workmate AI Agent", version="0.1.0")
for route in build_runtime_routes():
    app.router.routes.append(route)
app.include_router(internal_chat_router)
app.include_router(internal_chat_ui_router)


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
