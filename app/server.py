"""Windows PostgreSQL 개발 실행을 지원하는 Uvicorn 진입점."""

from __future__ import annotations

import asyncio
import os
import sys

import uvicorn


def main() -> None:
    """Windows에서는 psycopg가 지원하는 Selector Event Loop로 서버를 실행한다."""

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    uvicorn.run(
        "app.main:app",
        host=os.getenv("WORKMATE_HOST", "127.0.0.1"),
        port=int(os.getenv("WORKMATE_PORT", "8001")),
    )


if __name__ == "__main__":
    main()
