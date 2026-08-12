"""M0.1-01 runtime and route contract checks."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("WORKMATE_SERVICE_TOKEN", "test-token")

from fastapi.testclient import TestClient

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
        self.assertEqual(len(card["skills"]), 5)
        self.assertNotIn("mock", card["skills"][0]["tags"])
        self.assertEqual(
            card["securityRequirements"][0]["schemes"]["serviceBearer"].get(
                "list", []
            ),
            [],
        )

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


if __name__ == "__main__":
    unittest.main()
