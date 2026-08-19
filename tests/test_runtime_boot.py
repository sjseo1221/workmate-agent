"""M0.1-01 runtime and route contract checks."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("WORKMATE_SERVICE_TOKEN", "test-token")

from fastapi.testclient import TestClient

from app.a2a.runtime import ASSIGNEE_USER_IDS, RuntimeBootstrapExecutor
from app.main import app


class RuntimeBootTests(unittest.TestCase):
    """Verify that the official SDK runtime is mounted as the MVP contract."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_agent_card_is_public_and_declares_http_json_streaming(self) -> None:
        response = self.client.get("/.well-known/agent-card.json")
        self.assertEqual(response.status_code, 200)
        card = response.json()
        self.assertEqual(
            card["supportedInterfaces"][0]["protocolBinding"], "HTTP+JSON"
        )
        self.assertEqual(card["supportedInterfaces"][0]["protocolVersion"], "1.0")
        self.assertTrue(card["capabilities"]["streaming"])
        self.assertEqual(len(card["skills"]), 11)
        self.assertNotIn("mock", card["skills"][0]["tags"])
        self.assertEqual(
            card["securityRequirements"][0]["schemes"]["serviceBearer"].get(
                "list", []
            ),
            [],
        )

    def test_request_id_is_returned_for_correlation(self) -> None:
        response = self.client.get("/health/live", headers={"X-Request-ID": "req-log-1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-request-id"], "req-log-1")

    def test_readiness_requires_service_token(self) -> None:
        previous = os.environ.pop("WORKMATE_SERVICE_TOKEN", None)
        try:
            response = self.client.get("/health/ready")
            self.assertEqual(response.status_code, 503)
        finally:
            if previous is not None:
                os.environ["WORKMATE_SERVICE_TOKEN"] = previous

    def test_only_allowlisted_sdk_routes_are_mounted(self) -> None:
        paths = {
            (route.path, tuple(sorted(route.methods or [])))
            for route in app.routes
            if route.path.startswith("/a2a/")
        }
        self.assertIn(("/a2a/message:send", ("POST",)), paths)
        self.assertIn(("/a2a/message:stream", ("POST",)), paths)
        self.assertIn(("/a2a/tasks/{id}", ("GET", "HEAD")), paths)
        self.assertIn(("/a2a/tasks/{id}:cancel", ("POST",)), paths)
        self.assertIn(("/a2a/tasks/{id}:subscribe", ("POST",)), paths)
        self.assertNotIn("/a2a/tasks", {path for path, _ in paths})
        self.assertFalse(any("pushNotification" in path for path, _ in paths))

    def test_a2a_requires_service_token_and_version(self) -> None:
        payload = {
            "message": {
                "messageId": "message-1",
                "role": "ROLE_USER",
                "parts": [{"text": "runtime check"}],
            }
        }
        missing = self.client.post("/a2a/message:send", json=payload)
        self.assertEqual(missing.status_code, 401)

        headers = {"Authorization": "Bearer test-token"}
        wrong_version = self.client.post(
            "/a2a/message:send", json=payload, headers=headers
        )
        self.assertEqual(wrong_version.status_code, 400)

        response = self.client.post(
            "/a2a/message:send",
            json=payload,
            headers={**headers, "A2A-Version": "1.0"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["task"]["status"]["state"], "TASK_STATE_COMPLETED"
        )

    def test_default_cors_allows_local_standalone_ui_origin(self) -> None:
        """별도 폴더의 `workmate-ui` 개발 서버 Origin만 기본 허용한다."""

        response = self.client.get(
            "/health/live", headers={"Origin": "http://localhost:5175"}
        )
        self.assertEqual(
            response.headers.get("access-control-allow-origin"),
            "http://localhost:5175",
        )

    def test_cors_rejects_unlisted_origin(self) -> None:
        """목록에 없는 Origin은 CORS 헤더를 받지 않는다."""

        response = self.client.get(
            "/health/live", headers={"Origin": "http://evil.example.com"}
        )
        self.assertNotIn("access-control-allow-origin", response.headers)


class RuntimeUserIdResolutionTests(unittest.TestCase):
    """공개 A2A 요청의 실행 사용자를 정하는 `_resolve_user_id()`를 검증한다(2026-08-18).

    오케스트레이터가 요청 최상위 `metadata.owner`(담당자 이름, 20번 문서 R5)를 보내면
    그 이름을 `ASSIGNEE_USER_IDS`로 매핑해 쓰고, 없으면 기존 그대로 `"service"`로
    폴백한다. `RequestContext.metadata`는 SDK가 이미 dict로 변환해 주므로
    `_resolve_user_id`도 dict를 받는다 — `Message.metadata`가 아니라
    `SendMessageRequest.metadata`(요청 자체의 최상위 필드)라는 점이 실측으로 드러난
    버그였다(처음엔 `message.metadata`를 읽도록 잘못 구현해 실제로는 항상 폴백만 탔었다).
    """

    def test_explicit_payload_user_id_takes_priority_over_metadata_owner(self) -> None:
        """기존 호출자가 이미 `user_id`를 보내면 `metadata.owner`가 있어도 그걸 그대로 쓴다."""

        resolved = RuntimeBootstrapExecutor._resolve_user_id(
            {"user_id": "contract-user"}, {"owner": "서선정"}
        )
        self.assertEqual(resolved, "contract-user")

    def test_metadata_owner_resolves_to_the_matching_workmate_user_id(self) -> None:
        """`user_id`가 없을 때만 `metadata.owner`로 담당자를 구분한다.

        `ASSIGNEE_USER_IDS["서선정"]`와 비교하는 자기 참조 대신, 독립적으로 알고 있는
        실제 Google OIDC `sub` 리터럴과 직접 비교한다 — 2026-08-19 실사용 중, 이 상수에
        숫자 하나가 더 들어간 오타(21자리, 정상은 20자리)가 있었는데도 자기 참조 검증이라
        기존 테스트가 이를 못 잡아냈다. 오케스트레이터의 "Main Chatbot"이 `user_id` 없이
        `metadata.owner`만 보낼 때 이 오타 때문에 **다른 실제 사용자의 회의 데이터**로
        엉뚱하게 매핑돼, 마치 LLM이 없는 회의를 지어낸 것처럼 보이는 버그로 나타났다."""

        resolved = RuntimeBootstrapExecutor._resolve_user_id({}, {"owner": "서선정"})
        self.assertEqual(resolved, "10464531542706509691")

    def test_assignee_user_ids_matches_the_internal_chat_assignee_mapping_for_seo_seonjeong(self) -> None:
        """`internal_chat.ASSIGNEE_TO_USER_ID`(19번 문서 결정 5)와 이 모듈의
        `ASSIGNEE_USER_IDS`는 같은 4명을 각자 따로 하드코딩한 별도 상수다 — 하나만
        고치면 어긋난다(2026-08-19 실제로 "서선정" 값이 어긋나 있었다). 나머지 3명은
        두 파일에서 애초에 형식이 다른 placeholder(자릿수 맞춘 숫자 vs
        `dev-assignee-*` 문자열)라 값 자체가 같을 필요는 없다 — 실제 Workmate
        계정이 있는 "서선정"만 두 상수가 정확히 같은 값을 가리키는지 검증한다."""

        from app.internal_chat import ASSIGNEE_TO_USER_ID

        self.assertEqual(ASSIGNEE_USER_IDS["서선정"], ASSIGNEE_TO_USER_ID["서선정"])

    def test_unrecognized_or_missing_owner_falls_back_to_service(self) -> None:
        """모르는 이름이거나 `metadata.owner` 자체가 없으면 기존처럼 `"service"`를 쓴다."""

        self.assertEqual(RuntimeBootstrapExecutor._resolve_user_id({}, {"owner": "홍길동"}), "service")
        self.assertEqual(RuntimeBootstrapExecutor._resolve_user_id({}, {}), "service")
        self.assertEqual(RuntimeBootstrapExecutor._resolve_user_id({}, None), "service")


if __name__ == "__main__":
    unittest.main()
