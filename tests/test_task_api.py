"""M1.3 Task CRUD API 계약 테스트."""

from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.internal_chat import _authenticated_user_or_assignee
from app.main import app
from app.task_api import task_repository


class TaskApiTests(unittest.TestCase):
    """인증 사용자 범위, CRUD, 중복, soft delete를 검증한다."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.database_url = os.environ.pop("DATABASE_URL", None)
        os.environ["WORKMATE_TASK_DB_PATH"] = os.path.join(cls.temp_dir.name, "tasks.sqlite3")
        task_repository.cache_clear()
        app.dependency_overrides[_authenticated_user_or_assignee] = lambda: "user-a"
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        app.dependency_overrides.pop(_authenticated_user_or_assignee, None)
        task_repository.cache_clear()
        if cls.database_url is not None:
            os.environ["DATABASE_URL"] = cls.database_url
        os.environ.pop("WORKMATE_TASK_DB_PATH", None)
        cls.temp_dir.cleanup()

    def test_crud_and_soft_delete(self) -> None:
        response = self.client.post("/api/v1/tasks", json={"title": "보고서 작성"})
        self.assertEqual(response.status_code, 201)
        task = response.json()
        task_id = task["task_id"]
        self.assertEqual(task["assignee_user_id"], "user-a")

        listed = self.client.get("/api/v1/tasks")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([item["task_id"] for item in listed.json()], [task_id])

        updated = self.client.patch(
            f"/api/v1/tasks/{task_id}",
            json={"status": "in_progress", "priority_hint": 8},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["status"], "in_progress")

        deleted = self.client.delete(f"/api/v1/tasks/{task_id}")
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(self.client.get(f"/api/v1/tasks/{task_id}").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/tasks").json(), [])

    def test_external_source_is_idempotent_and_scope_is_enforced(self) -> None:
        payload = {"title": "메일 회신", "source_type": "email", "source_id": "msg-1"}
        first = self.client.post("/api/v1/tasks", json=payload)
        second = self.client.post("/api/v1/tasks", json=payload)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.json()["task_id"], second.json()["task_id"])

        app.dependency_overrides[_authenticated_user_or_assignee] = lambda: "user-b"
        self.assertEqual(self.client.get(f"/api/v1/tasks/{first.json()['task_id']}").status_code, 404)
        app.dependency_overrides[_authenticated_user_or_assignee] = lambda: "user-a"

    def test_due_before_filters_out_later_or_unset_deadlines(self) -> None:
        """마감일 필터(#17) — `due_before` 이후 마감이거나 마감이 없는 Task는 제외한다."""

        near = self.client.post("/api/v1/tasks", json={"title": "곧 마감", "due_at": "2026-08-16T00:00:00+09:00"})
        far = self.client.post("/api/v1/tasks", json={"title": "먼 마감", "due_at": "2026-12-31T00:00:00+09:00"})
        no_due = self.client.post("/api/v1/tasks", json={"title": "마감 없음"})
        try:
            listed = self.client.get("/api/v1/tasks", params={"due_before": "2026-08-20T00:00:00+09:00"})
            self.assertEqual(listed.status_code, 200)
            ids = {item["task_id"] for item in listed.json()}
            self.assertIn(near.json()["task_id"], ids)
            self.assertNotIn(far.json()["task_id"], ids)
            self.assertNotIn(no_due.json()["task_id"], ids)
        finally:
            for response in (near, far, no_due):
                self.client.delete(f"/api/v1/tasks/{response.json()['task_id']}")

    def test_invalid_source_is_rejected(self) -> None:
        response = self.client.post(
            "/api/v1/tasks",
            json={"title": "잘못된 Task", "source_type": "manual", "source_id": "x"},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
