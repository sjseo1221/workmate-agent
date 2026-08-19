"""M0.4-03 수동 UI 검수용 로컬 OIDC·WORKING Task Fixture.

이 파일은 운영 Route나 공개 Schema를 변경하지 않는다. 직접 실행할 때만
내부 검증 API의 저장소와 Workflow 경계를 테스트용 객체로 주입해 실제
브라우저에서 Polling·취소 상태를 확인할 수 있게 한다.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from typing import Any

import jwt
import uvicorn
from a2a.server.context import ServerCallContext
from a2a.types import Task, TaskState, TaskStatus
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.workflows.registry import WorkflowResult


class _JwksHandler(BaseHTTPRequestHandler):
    """로컬 OIDC 검증에 사용할 공개 키만 반환하는 테스트 서버다."""

    jwks = b"{}"

    def do_GET(self) -> None:  # noqa: N802 - 표준 라이브러리 Handler 계약
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.jwks)))
        self.end_headers()
        self.wfile.write(self.jwks)

    def log_message(self, format: str, *args: object) -> None:
        """검수용 JWKS 요청 로그를 출력하지 않는다."""


class WorkingTaskFixtureStore:
    """한 사용자 범위의 `WORKING` Task를 메모리에 보관하는 테스트 Store다."""

    def __init__(self) -> None:
        self._tasks: dict[str, tuple[str, Task]] = {}

    @staticmethod
    def _owner(context: ServerCallContext) -> str:
        user = getattr(context, "user", None)
        return str(getattr(user, "user_name", ""))

    async def save(self, task: Task, context: ServerCallContext) -> None:
        """Task를 OIDC 사용자 범위와 함께 저장한다."""

        self._tasks[task.id] = (self._owner(context), task)

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        """동일한 OIDC 사용자만 Fixture Task를 조회할 수 있게 한다."""

        stored = self._tasks.get(task_id)
        if stored is None or stored[0] != self._owner(context):
            return None
        return stored[1]

    async def record_message(
        self,
        message_id: str,
        task_id: str,
        payload: object,
        response: Task | None = None,
    ) -> bool:
        """UI 검수에는 메시지 이력보다 Task 상태가 필요하므로 성공만 반환한다."""

        return True

    async def save_artifact(self, task_id: str, artifact: object) -> None:
        """Fixture에서는 Artifact를 별도 저장하지 않는다."""

    async def mark_cancel_requested(self, task_id: str) -> None:
        """진행 중 Fixture Task를 취소 상태로 전환한다."""

        stored = self._tasks.get(task_id)
        if stored is None:
            return
        stored[1].status.state = TaskState.TASK_STATE_CANCELED


class WorkingWorkflowFixture:
    """내부 UI POST를 즉시 완료하지 않고 `WORKING`으로 유지하는 Registry다."""

    async def execute(self, request: Any) -> WorkflowResult:
        """실제 업무 호출 없이 취소 가능한 검수용 결과를 반환한다."""

        return WorkflowResult(
            artifact_name="ui_working_fixture",
            artifact_description="Test-only WORKING Task; no business result.",
            text="UI cancellation fixture is waiting for cancellation.",
            state="working",
            mock=True,
        )


def _token(private_key: rsa.RSAPrivateKey, subject: str = "ui-fixture-user") -> str:
    """브라우저에 한 번 붙여 넣을 로컬 OIDC JWT를 만든다."""

    return jwt.encode(
        {
            "sub": subject,
            "iss": "https://workmate-ui-fixture.local",
            "aud": "workmate-ui-fixture",
            "exp": time.time_ns() // 1_000_000_000 + 3600,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "workmate-ui-fixture-key"},
    )


@contextmanager
def install_fixture() -> Iterator[str]:
    """OIDC 환경과 내부 API 테스트 객체를 설치하고 토큰을 반환한다."""

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk["kid"] = "workmate-ui-fixture-key"
    _JwksHandler.jwks = json.dumps({"keys": [public_jwk]}).encode("utf-8")
    jwks_server = ThreadingHTTPServer(("127.0.0.1", 0), _JwksHandler)
    jwks_thread = threading.Thread(target=jwks_server.serve_forever, daemon=True)
    jwks_thread.start()

    os.environ.update(
        {
            "WORKMATE_INTERNAL_CHAT_ENABLED": "true",
            "WORKMATE_OIDC_ISSUER": "https://workmate-ui-fixture.local",
            "WORKMATE_OIDC_AUDIENCE": "workmate-ui-fixture",
            "WORKMATE_OIDC_JWKS_URL": (
                f"http://127.0.0.1:{jwks_server.server_port}/jwks.json"
            ),
        }
    )

    import app.internal_chat as internal_chat

    store = WorkingTaskFixtureStore()
    original_store = internal_chat.task_store
    original_registry = internal_chat.workflow_registry
    original_persist = internal_chat._persist_task
    internal_chat.task_store = lambda: store
    internal_chat.workflow_registry = lambda: WorkingWorkflowFixture()

    async def persist_working_task(task_id, request, result, user_id):
        task = Task(
            id=task_id,
            context_id=task_id,
            status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
        )
        await store.save(
            task,
            ServerCallContext(user=internal_chat._InternalUser(user_id)),
        )
        return task

    internal_chat._persist_task = persist_working_task
    try:
        yield _token(private_key)
    finally:
        internal_chat.task_store = original_store
        internal_chat.workflow_registry = original_registry
        internal_chat._persist_task = original_persist
        for name in (
            "WORKMATE_INTERNAL_CHAT_ENABLED",
            "WORKMATE_OIDC_ISSUER",
            "WORKMATE_OIDC_AUDIENCE",
            "WORKMATE_OIDC_JWKS_URL",
        ):
            os.environ.pop(name, None)
        jwks_server.shutdown()
        jwks_server.server_close()
        jwks_thread.join(timeout=5)


def main() -> None:
    """Fixture 서버를 실행하고 브라우저 검수 정보를 출력한다."""

    from app.main import app

    with install_fixture() as token:
        print("UI: http://127.0.0.1:8010/internal/skill-chat", flush=True)
        print(
            "OIDC Bearer Token (저장하지 말고 현재 검수 탭에만 붙여 넣으세요):",
            flush=True,
        )
        print(token, flush=True)
        print("종료: Ctrl+C", flush=True)
        try:
            uvicorn.run(app, host="127.0.0.1", port=8010, log_level="info")
        finally:
            pass


if __name__ == "__main__":
    main()
