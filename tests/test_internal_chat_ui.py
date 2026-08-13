"""내부 검증 UI의 진입과 입력 요소를 검증한다."""

from __future__ import annotations

import os
import unittest

from fastapi.testclient import TestClient

from app.main import app


class InternalChatUiTests(unittest.TestCase):
    """UI가 API 계약에 필요한 입력 컨트롤을 제공하는지 확인한다."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def tearDown(self) -> None:
        os.environ.pop("WORKMATE_INTERNAL_CHAT_ENABLED", None)

    def test_page_contains_skill_and_input_controls(self) -> None:
        os.environ["WORKMATE_INTERNAL_CHAT_ENABLED"] = "true"
        response = self.client.get("/internal/skill-chat")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="skill"', response.text)
        self.assertIn('id="mode"', response.text)
        self.assertIn('id="input"', response.text)
        self.assertIn("/api/v1/internal/skill-chat/messages", response.text)
        self.assertIn("token.addEventListener('input'", response.text)
        self.assertIn("Token 입력 후 Skill 목록을 불러오세요", response.text)

    def test_page_is_hidden_when_internal_chat_is_disabled(self) -> None:
        response = self.client.get("/internal/skill-chat")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
