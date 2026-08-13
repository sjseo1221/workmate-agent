"""M0.3-01 내부 검증 챗봇 입력 API 테스트."""

from __future__ import annotations

import os
import unittest

from fastapi.testclient import TestClient

from app.main import app


class InternalChatInputTests(unittest.TestCase):
    """Skill 목록과 자연어·JSON 입력 경계를 검증한다."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def setUp(self) -> None:
        os.environ["WORKMATE_INTERNAL_CHAT_ENABLED"] = "true"

    def tearDown(self) -> None:
        os.environ.pop("WORKMATE_INTERNAL_CHAT_ENABLED", None)

    def test_lists_the_same_five_skills_as_agent_card(self) -> None:
        response = self.client.get("/api/v1/internal/skill-chat/skills")
        self.assertEqual(response.status_code, 200)
        card = self.client.get("/.well-known/agent-card.json")
        self.assertEqual(card.status_code, 200)
        self.assertEqual(
            [skill["id"] for skill in response.json()],
            [skill["id"] for skill in card.json()["skills"]],
        )
        self.assertEqual(len(response.json()), 5)

    def test_accepts_natural_language_and_json_input(self) -> None:
        natural = self.client.post(
            "/api/v1/internal/skill-chat/messages",
            json={"skill_id": "weekly_report", "input": "지난주 보고서 요약"},
        )
        self.assertEqual(natural.status_code, 200)
        self.assertEqual(natural.json()["input_type"], "natural_language")

        structured = self.client.post(
            "/api/v1/internal/skill-chat/messages",
            json={
                "skill_id": "search_meetings",
                "input": {
                    "schema_version": "1.0",
                    "skill_id": "search_meetings",
                    "query": "결정사항",
                },
            },
        )
        self.assertEqual(structured.status_code, 200)
        self.assertEqual(structured.json()["input_type"], "json")

    def test_rejects_unknown_skill_blank_input_and_mismatched_json_skill(self) -> None:
        unknown = self.client.post(
            "/api/v1/internal/skill-chat/messages",
            json={"skill_id": "unknown", "input": "hello"},
        )
        self.assertEqual(unknown.status_code, 422)

        blank = self.client.post(
            "/api/v1/internal/skill-chat/messages",
            json={"skill_id": "weekly_report", "input": "   "},
        )
        self.assertEqual(blank.status_code, 422)

        mismatch = self.client.post(
            "/api/v1/internal/skill-chat/messages",
            json={
                "skill_id": "weekly_report",
                "input": {"skill_id": "daily_briefing"},
            },
        )
        self.assertEqual(mismatch.status_code, 422)

    def test_disabled_api_is_not_available(self) -> None:
        os.environ.pop("WORKMATE_INTERNAL_CHAT_ENABLED", None)
        response = self.client.get("/api/v1/internal/skill-chat/skills")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
