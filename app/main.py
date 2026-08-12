"""FastAPI entrypoint for the Workmate A2A agent."""

from __future__ import annotations

import hmac

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.a2a.runtime import (
    A2A_VERSION,
    build_runtime_routes,
    is_a2a_path,
    service_token,
)


app = FastAPI(title="Workmate AI Agent", version="0.1.0")
for route in build_runtime_routes():
    app.router.routes.append(route)


@app.middleware("http")
async def verify_a2a_headers(request: Request, call_next):
    """Protect SDK A2A routes with the existing service-token contract."""

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
    return await call_next(request)


@app.get("/health/live")
def health_live() -> dict[str, str]:
    """Return liveness without requiring provider credentials."""

    return {"status": "alive"}


@app.get("/health/ready")
def health_ready() -> dict[str, str]:
    """Return readiness only when the service token is configured."""

    if not service_token():
        return JSONResponse(
            {"status": "not_ready", "reason": "service_token_missing"},
            status_code=503,
        )
    return {"status": "ready"}
