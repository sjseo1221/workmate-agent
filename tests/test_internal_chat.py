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
from a2a.server.context import ServerCallContext
from a2a.types import Task, TaskState, TaskStatus
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

    def test_lists_the_same_eleven_skills_as_the_public_agent_card(self) -> None:
        """내부 검증 API(Drawer 포함)와 Agent Card는 이제 같은 11개 목록이다
        (2026-08-17, 15번 문서 결정으로 18번 문서 "핵심 설계 결정 1"을 뒤집어
        Workmate 어시스턴트 전용이던 6개도 Orchestrator 공개 계약에 합류시켰다,
        20번 문서 1단계)."""

        response = self.client.get("/api/v1/internal/skill-chat/skills")
        self.assertEqual(response.status_code, 200)
        card = self.client.get("/.well-known/agent-card.json")
        self.assertEqual(card.status_code, 200)
        internal_ids = [skill["id"] for skill in response.json()]
        card_ids = [skill["id"] for skill in card.json()["skills"]]
        self.assertEqual(card_ids, internal_ids)
        self.assertEqual(
            internal_ids,
            [
                "daily_briefing", "weekly_report", "analyze_meeting", "search_meetings", "rank_priorities",
                "get_meeting_analysis", "review_action_items", "review_proposal", "manage_tasks", "read_email", "assistant_ask",
            ],
        )

    def test_accepts_natural_language_and_json_input(self) -> None:
        natural = self.client.post(
            "/api/v1/internal/skill-chat/messages",
            json={"skill_id": "weekly_report", "input": "지난주 보고서 요약"},
        )
        self.assertEqual(natural.status_code, 200)
        self.assertEqual(natural.json()["input_type"], "natural_language")
        self.assertEqual(natural.json()["state"], "completed")
        self.assertFalse(natural.json()["artifact"]["mock"])
        self.assertTrue(natural.json()["artifact"]["business_result"])
        task_id = natural.json()["task_id"]
        task = self.client.get(f"/api/v1/internal/skill-chat/tasks/{task_id}")
        self.assertEqual(task.status_code, 200)
        self.assertEqual(task.json()["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertFalse(task.json()["artifacts"][0]["metadata"]["mock"])
        self.assertTrue(task.json()["artifacts"][0]["metadata"]["business_result"])
        cancel = self.client.post(f"/api/v1/internal/skill-chat/tasks/{task_id}:cancel")
        self.assertEqual(cancel.status_code, 409)

        search_result = WorkflowResult(
            artifact_name="grounded_answer",
            artifact_description="내부 입력 형식 검증용 검색 결과",
            text="근거가 충분하지 않습니다.",
            data={
                "type": "grounded_answer",
                "data": {
                    "answer": "근거가 충분하지 않습니다.",
                    "sources": [],
                    "insufficient_evidence": True,
                },
            },
            mock=False,
        )
        with patch("app.internal_chat.workflow_registry") as registry:
            registry.return_value.execute = AsyncMock(return_value=search_result)
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

    def test_cancel_endpoint_marks_working_fixture_cancelled(self) -> None:
        class WorkingTaskFixture:
            def __init__(self) -> None:
                self.task = Task(
                    id="working-fixture",
                    context_id="working-fixture",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )

            async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
                return self.task if task_id == self.task.id else None

            async def mark_cancel_requested(self, task_id: str) -> None:
                self.task.status.state = TaskState.TASK_STATE_CANCELED

        fixture = WorkingTaskFixture()
        with patch("app.internal_chat.task_store", return_value=fixture):
            response = self.client.post(
                "/api/v1/internal/skill-chat/tasks/working-fixture:cancel"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"]["state"], "TASK_STATE_CANCELED")

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

    def test_workflow_value_error_becomes_a_cors_visible_422(self) -> None:
        """Workflow가 잘못된 입력·상태를 관례대로 `ValueError`로 표시하면
        (동기 Skill 기준) 이를 잡아 422로 변환해야 한다. 잡지 않으면 CORS
        헤더 없는 500이 나가 브라우저에서는 실제 오류 대신 `Failed to
        fetch`만 보인다 — 2026-08-15 실사용 중 재현·확인된 버그.

        `analyze_meeting`은 2026-08-16(17번 갭 문서 #7)부터 비동기 Task
        Polling 경로를 타 이 동기 422 계약 밖이다 —
        `test_analyze_meeting_value_error_surfaces_as_a_failed_task`가
        그 경로를 별도로 검증한다. 여기서는 여전히 동기인 `search_meetings`로
        같은 오류 변환 규칙을 검증한다."""

        with patch("app.internal_chat.workflow_registry") as registry:
            registry.return_value.execute = AsyncMock(side_effect=ValueError("query must not be blank"))
            response = self.client.post(
                "/api/v1/internal/skill-chat/messages",
                json={"skill_id": "search_meetings", "input": "회의 검색"},
                headers={"Origin": "http://localhost:3000"},
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "query must not be blank")
        # CORS 헤더가 실제로 붙어 있어야 브라우저가 이 응답을 읽을 수 있다.
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://localhost:3000")

    def test_workflow_runtime_error_becomes_a_cors_visible_503(self) -> None:
        """Workflow 계층은 인프라/설정 미비(예: `search_meetings`의
        `DATABASE_URL is required`)를 관례대로 `RuntimeError`로 표시한다.
        ValueError와 같은 이유로 이를 잡지 않으면 CORS 헤더 없는 500이 나가
        `Failed to fetch`만 보인다 — 2026-08-15 실사용 중 재현·확인된 버그
        (필터를 적용한 회의록 검색에서 발생)."""

        with patch("app.internal_chat.workflow_registry") as registry:
            registry.return_value.execute = AsyncMock(
                side_effect=RuntimeError("DATABASE_URL is required for search_meetings")
            )
            response = self.client.post(
                "/api/v1/internal/skill-chat/messages",
                json={"skill_id": "search_meetings", "input": "QA 빌드 일정은 언제 결정됐어?"},
                headers={"Origin": "http://localhost:3000"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "DATABASE_URL is required for search_meetings")
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://localhost:3000")

    def test_jwks_network_failure_becomes_a_cors_visible_503(self) -> None:
        """JWKS Key를 가져오는 실제 네트워크 호출이 실패하면(DNS·연결 재설정 등)
        `jwt.PyJWTError`가 아닌 원본 네트워크 예외가 그대로 올라온다. 예전엔 이를
        잡지 않아 CORS 없는 500이 나가 "Failed to fetch"로 보였다 — 2026-08-15
        실사용 중 오늘 브리핑의 두 동시 요청이 나란히 이 오류를 맞아 화면이
        "브리핑을 아직 실행하지 않았습니다"에 멈춰 있는 것으로 재현·확인됐다."""

        from urllib.error import URLError

        with patch("app.internal_chat._jwks_client") as jwks_client:
            jwks_client.return_value.get_signing_key_from_jwt.side_effect = URLError("network is unreachable")
            response = self.client.post(
                "/api/v1/internal/skill-chat/messages",
                json={"skill_id": "daily_briefing", "input": "오늘 브리핑"},
                headers={"Origin": "http://localhost:3000"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://localhost:3000")

    def test_analyze_meeting_returns_submitted_immediately_then_polls_to_completed(self) -> None:
        """`analyze_meeting`은 즉시 `state="submitted"`로 응답하고, 실제 STT+LLM
        실행은 백그라운드에서 끝난 뒤 Task Polling으로 드러나야 한다
        (2026-08-16, 17번 갭 문서 #7). `TestClient`는 `BackgroundTasks`를 응답
        직후 같은 호출 안에서 실행하므로, `client.post`가 돌아온 시점에는
        이미 아래 결과로 Task가 갱신돼 있다."""

        result = WorkflowResult(
            artifact_name="meeting_analysis",
            artifact_description="회의 분석 결과",
            text="분석이 끝났습니다.",
            data={"type": "meeting_analysis", "data": {"meeting_id": "m-1", "summary": "요약", "action_items": [], "transcript_ref": "t-1", "source_refs": []}},
            mock=False,
        )
        with patch("app.internal_chat.workflow_registry") as registry:
            registry.return_value.execute = AsyncMock(return_value=result)
            submitted = self.client.post(
                "/api/v1/internal/skill-chat/messages",
                json={"skill_id": "analyze_meeting", "input": "회의 분석 실행"},
            )
        self.assertEqual(submitted.status_code, 200)
        self.assertEqual(submitted.json()["state"], "submitted")
        task_id = submitted.json()["task_id"]

        completed = self.client.get(f"/api/v1/internal/skill-chat/tasks/{task_id}")
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.json()["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertFalse(completed.json()["artifacts"][0]["metadata"]["mock"])

    def test_analyze_meeting_value_error_surfaces_as_a_failed_task(self) -> None:
        """동기 Skill이라면 422로 바뀌었을 `ValueError`가, 비동기 경로에서는
        `FAILED` Task의 `status.message`에 담겨 Polling으로 드러나야 한다
        (2026-08-16, 17번 갭 문서 #7)."""

        with patch("app.internal_chat.workflow_registry") as registry:
            registry.return_value.execute = AsyncMock(side_effect=ValueError("final transcript is required"))
            submitted = self.client.post(
                "/api/v1/internal/skill-chat/messages",
                json={"skill_id": "analyze_meeting", "input": "회의 분석 실행"},
            )
        self.assertEqual(submitted.status_code, 200)
        task_id = submitted.json()["task_id"]

        failed = self.client.get(f"/api/v1/internal/skill-chat/tasks/{task_id}")
        self.assertEqual(failed.status_code, 200)
        self.assertEqual(failed.json()["status"]["state"], "TASK_STATE_FAILED")
        self.assertEqual(failed.json()["status"]["message"]["parts"][0]["text"], "final transcript is required")

    def test_assistant_ask_is_also_async_and_carries_a_parseable_reply_envelope(self) -> None:
        """`assistant_ask`(자연어 Router, 2026-08-17)도 `analyze_meeting`처럼
        Task Polling 경로를 탄다 — 어떤 Skill로 풀릴지 호출 전엔 모르고,
        `analyze_meeting`으로 풀리면 STT+LLM만큼 오래 걸릴 수 있어서다
        (`app/internal_chat.py`의 `_ASYNC_SKILLS` Docstring 참고). Task Store는
        `text`만 저장하므로, 그 `text`가 프론트가 파싱할 수 있는 답변 Envelope
        JSON인지 확인한다."""

        result = WorkflowResult(
            artifact_name="assistant_ask",
            artifact_description="자연어 답변",
            text=json.dumps({"type": "assistant_reply", "data": {"reply": "오늘 일정은 없어요.", "executed_skill_id": "daily_briefing", "pending_action": None}}, ensure_ascii=False),
            data={"type": "assistant_reply", "data": {"reply": "오늘 일정은 없어요.", "executed_skill_id": "daily_briefing", "pending_action": None}},
            markdown="오늘 일정은 없어요.",
            mock=False,
        )
        with patch("app.internal_chat.workflow_registry") as registry:
            registry.return_value.execute = AsyncMock(return_value=result)
            submitted = self.client.post(
                "/api/v1/internal/skill-chat/messages",
                json={
                    "skill_id": "assistant_ask",
                    "input": {
                        "schema_version": "1.0",
                        "skill_id": "assistant_ask",
                        "user_id": "contract-user",
                        "text": "오늘 브리핑 보여줘",
                    },
                },
            )
        self.assertEqual(submitted.status_code, 200)
        self.assertEqual(submitted.json()["state"], "submitted")
        task_id = submitted.json()["task_id"]

        completed = self.client.get(f"/api/v1/internal/skill-chat/tasks/{task_id}")
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.json()["status"]["state"], "TASK_STATE_COMPLETED")
        envelope = json.loads(completed.json()["artifacts"][0]["parts"][0]["text"])
        self.assertEqual(envelope["data"]["reply"], "오늘 일정은 없어요.")
        self.assertIsNone(envelope["data"]["pending_action"])

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
