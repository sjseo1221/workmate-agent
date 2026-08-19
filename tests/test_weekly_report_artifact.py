"""M2.3-02 주간 보고서 A2A Artifact 변환 계약 테스트."""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.main import app
from tests.test_contract import _sdk_message, _skill_inputs, _validator


class WeeklyReportArtifactTests(unittest.TestCase):
    """주간 보고서가 구조화 JSON과 복사용 Markdown을 함께 제공하는지 검증한다."""

    def test_public_artifact_contains_valid_json_and_markdown_parts(self) -> None:
        """공개 Route의 두 Part와 승인된 Schema 준수를 확인한다."""

        data = next(item for item in _skill_inputs() if item["skill_id"] == "weekly_report")
        response = TestClient(app).post(
            "/a2a/message:send",
            json=_sdk_message("weekly_report", data),
            headers={
                "Authorization": "Bearer test-token",
                "A2A-Version": "1.0",
                "Content-Type": "application/a2a+json",
            },
        )

        self.assertEqual(response.status_code, 200)
        artifact = response.json()["task"]["artifacts"][0]
        _validator("a2a-artifact.schema.json").validate(artifact)

        parts = artifact["parts"]
        self.assertEqual([part["mediaType"] for part in parts], ["application/json", "text/markdown"])
        self.assertEqual(parts[0]["data"]["type"], "weekly_report")
        _validator("workmate-skill-schemas.schema.json").validate(
            {
                "schema_version": "1.0",
                "request_id": "request-weekly-artifact",
                "generated_at": "2026-08-12T00:00:00Z",
                "data_freshness": {"task": None, "calendar": None, "email": None, "meeting": None},
                "warnings": [],
                "result": parts[0]["data"],
            }
        )
        self.assertTrue(parts[1]["text"].startswith("# 주간 보고서\n"))
        self.assertIn("## 다음 주 계획", parts[1]["text"])


if __name__ == "__main__":
    unittest.main()
