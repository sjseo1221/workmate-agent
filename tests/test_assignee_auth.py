"""19번 문서 결정 5 — 담당자 이름 기반 우회 인증(`_authenticated_user_or_assignee`) 계약 테스트.

`Authorization` 헤더가 없을 때만 `X-Workmate-Assignee`를 신뢰하고, 있으면(설령 무효해도)
그 결과를 그대로 쓴다는 우선순위 규칙을 검증한다 — 그렇지 않으면 아무 토큰이나 넣고
담당자 이름을 사칭할 수 있다.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from urllib.parse import quote

from fastapi.testclient import TestClient

from app.internal_chat import ASSIGNEE_TO_USER_ID
from app.main import app
from app.task_api import task_repository


class AssigneeAuthTests(unittest.TestCase):
    """`task_api.py`(우회 대상)를 통해 실제 HTTP 경계에서 검증한다."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.database_url = os.environ.pop("DATABASE_URL", None)
        os.environ["WORKMATE_TASK_DB_PATH"] = os.path.join(cls.temp_dir.name, "tasks.sqlite3")
        task_repository.cache_clear()
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        task_repository.cache_clear()
        if cls.database_url is not None:
            os.environ["DATABASE_URL"] = cls.database_url
        os.environ.pop("WORKMATE_TASK_DB_PATH", None)
        cls.temp_dir.cleanup()

    def test_missing_authorization_and_missing_assignee_header_is_401(self) -> None:
        response = self.client.get("/api/v1/tasks")
        self.assertEqual(response.status_code, 401)

    def test_unknown_assignee_name_is_400(self) -> None:
        response = self.client.get("/api/v1/tasks", headers={"X-Workmate-Assignee": quote("모르는사람")})
        self.assertEqual(response.status_code, 400)

    def test_non_percent_encoded_header_value_is_400_not_a_transport_crash(self) -> None:
        """헤더 값은 ASCII 밖 문자를 담지 못한다(httpx·브라우저 fetch 둘 다 예외) —
        오케스트레이터 프론트가 `encodeURIComponent`를 빼먹었을 때는 percent-decode
        실패로 400을 내야지, 서버가 깨지면 안 된다."""

        response = self.client.get("/api/v1/tasks", headers={"X-Workmate-Assignee": "%EC%84%9C%FF"})
        self.assertEqual(response.status_code, 400)

    def test_each_known_assignee_resolves_to_its_fixed_user_id(self) -> None:
        for name, expected_user_id in ASSIGNEE_TO_USER_ID.items():
            with self.subTest(assignee=name):
                encoded = quote(name)
                created = self.client.post(
                    "/api/v1/tasks",
                    json={"title": f"{name}의 Task"},
                    headers={"X-Workmate-Assignee": encoded},
                )
                self.assertEqual(created.status_code, 201)
                listed = self.client.get("/api/v1/tasks", headers={"X-Workmate-Assignee": encoded})
                self.assertEqual(listed.status_code, 200)
                self.assertTrue(any(item["title"] == f"{name}의 Task" for item in listed.json()))
                # 다른 담당자 헤더로는 이 Task가 안 보여야 한다(고정 user_id로 스코프 분리).
                other_name = next(n for n in ASSIGNEE_TO_USER_ID if n != name)
                cross_scope = self.client.get("/api/v1/tasks", headers={"X-Workmate-Assignee": quote(other_name)})
                self.assertFalse(any(item["title"] == f"{name}의 Task" for item in cross_scope.json()))
                self.assertNotEqual(expected_user_id, "")

    def test_authorization_header_present_but_invalid_does_not_fall_back_to_assignee(self) -> None:
        """`Authorization`이 있으면(설령 무효해도) 조용히 담당자 헤더로 넘어가지 않는다 —
        그렇지 않으면 아무 토큰이나 넣고 담당자 이름을 사칭할 수 있다."""

        response = self.client.get(
            "/api/v1/tasks",
            headers={
                "Authorization": "Bearer not-a-real-token",
                "X-Workmate-Assignee": quote("서선정"),
            },
        )
        self.assertIn(response.status_code, (401, 503))


if __name__ == "__main__":
    unittest.main()
