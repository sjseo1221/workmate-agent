"""M1.3-02 Task 관리 UI 계약 테스트."""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.main import app


class TaskUiTests(unittest.TestCase):
    """화면에 CRUD 입력과 API 연결 요소가 존재하는지 검증한다."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_task_page_contains_crud_controls(self) -> None:
        response = self.client.get("/tasks")
        self.assertEqual(response.status_code, 200)
        for element in ('id="token"', 'id="title"', 'id="status"', 'id="priority"', 'id="dueAt"', 'id="save"', 'id="refresh"'):
            self.assertIn(element, response.text)
        self.assertIn("/api/v1/tasks", response.text)
        self.assertIn("method:id?'PATCH':'POST'", response.text)
        self.assertIn("method:'DELETE'", response.text)
        self.assertIn("confirm('이 Task를 삭제할까요?')", response.text)


if __name__ == "__main__":
    unittest.main()
