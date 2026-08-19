"""M2.3 주간 보고서 공개 전송과 영속 Task 통합 검수."""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.main import app
from tests.test_contract import _sdk_message, _skill_inputs, _validator


class M23IntegrationTests(unittest.TestCase):
    """공개 요청부터 Schema Artifact와 Task 재조회까지 검증한다."""

    def test_weekly_report_artifact_survives_task_lookup_without_mock(self) -> None:
        """완료 Task 재조회가 같은 비 Mock 보고서 Artifact를 반환해야 한다."""

        client = TestClient(app)
        headers = {
            "Authorization": "Bearer test-token",
            "A2A-Version": "1.0",
            "Content-Type": "application/a2a+json",
        }
        data = next(item for item in _skill_inputs() if item["skill_id"] == "weekly_report")
        sent = client.post(
            "/a2a/message:send",
            json=_sdk_message("weekly_report", data),
            headers=headers,
        )
        self.assertEqual(sent.status_code, 200)
        task = sent.json()["task"]

        fetched = client.get(f"/a2a/tasks/{task['id']}", headers=headers)
        self.assertEqual(fetched.status_code, 200)
        persisted = fetched.json()
        self.assertEqual(persisted["id"], task["id"])
        self.assertEqual(persisted["artifacts"], task["artifacts"])

        artifact = persisted["artifacts"][0]
        _validator("a2a-artifact.schema.json").validate(artifact)
        self.assertEqual(artifact["name"], "weekly_report")
        self.assertNotIn("mock", artifact)
        self.assertEqual(artifact["parts"][0]["data"]["type"], "weekly_report")


if __name__ == "__main__":
    unittest.main()

