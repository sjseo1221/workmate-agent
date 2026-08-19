"""M3.1 회의 입력 API 검증."""

import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.internal_chat import _authenticated_user_or_assignee
from app.main import app
from app.meeting_api import meeting_repository
from tests.fixtures.fake_r2 import FakeR2Storage


class MeetingApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["WORKMATE_MEETING_DB_PATH"] = os.path.join(cls.tmp.name, "meetings.sqlite3")
        meeting_repository.cache_clear()
        app.dependency_overrides[_authenticated_user_or_assignee] = lambda: "user-a"
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(_authenticated_user_or_assignee, None)
        meeting_repository.cache_clear()
        os.environ.pop("WORKMATE_MEETING_DB_PATH", None)
        cls.tmp.cleanup()

    def setUp(self):
        self.fake_r2 = FakeR2Storage()

    def test_create_scope_and_duplicate_upload(self):
        created = self.client.post("/api/v1/meetings", json={"meeting_id": "m-1", "title": "주간 회의"})
        self.assertEqual(created.status_code, 201)
        duplicate = self.client.post("/api/v1/meetings", json={"meeting_id": "m-1", "title": "주간 회의"})
        self.assertEqual(duplicate.status_code, 201)
        body = {"file": ("note.wav", b"audio-bytes", "audio/wav")}
        with patch("app.meeting_api.r2_storage", return_value=self.fake_r2):
            first = self.client.post("/api/v1/meetings/m-1/recordings", files=body)
            second = self.client.post("/api/v1/meetings/m-1/recordings", files=body)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json()["recording_id"], second.json()["recording_id"])
        self.assertEqual(len(self.fake_r2.objects), 1, "동일 checksum 재업로드는 같은 Key를 재사용해야 함")

    def test_upload_stores_bytes_in_r2_and_confirms_source_audio_uri(self):
        self.client.post("/api/v1/meetings", json={"meeting_id": "m-audio", "title": "R2 저장 확인"})
        body = {"file": ("note.wav", b"real-audio-bytes-not-lost", "audio/wav")}
        with patch("app.meeting_api.r2_storage", return_value=self.fake_r2):
            response = self.client.post("/api/v1/meetings/m-audio/recordings", files=body)
        self.assertEqual(response.status_code, 201)
        object_key = response.json()["object_key"]
        self.assertIsNotNone(object_key)
        # 메타데이터만이 아니라 실제 바이트가 R2(Fake)에 저장돼야 한다 —
        # 2026-08-15 이전엔 이 부분이 통째로 없었다(#20).
        self.assertEqual(self.fake_r2.get_object(object_key), b"real-audio-bytes-not-lost")
        meeting = meeting_repository().get("m-audio", "user-a")
        self.assertEqual(meeting.source_audio_uri, object_key)

    def test_upload_rejects_when_r2_not_configured(self):
        self.client.post("/api/v1/meetings", json={"meeting_id": "m-noR2", "title": "R2 미설정"})
        body = {"file": ("note.wav", b"x", "audio/wav")}
        with patch("app.meeting_api.r2_storage", side_effect=RuntimeError("R2 configuration is missing")):
            response = self.client.post("/api/v1/meetings/m-noR2/recordings", files=body)
        self.assertEqual(response.status_code, 503)

    def test_invalid_mime_and_missing_meeting(self):
        self.assertEqual(self.client.post("/api/v1/meetings", json={"title": "회의"}).status_code, 201)
        self.assertEqual(self.client.post("/api/v1/meetings/missing/recordings", files={"file": ("x.txt", b"x", "text/plain")}).status_code, 404)
        self.assertEqual(self.client.post("/api/v1/meetings/missing/recordings", files={"file": ("x.txt", b"x", "text/plain")}).status_code, 404)

    def test_delete_removes_the_meeting_from_list_and_detail(self):
        """회의 삭제 기능 추가(2026-08-17, 사용자 요청).

        상세 조회(`GET /{id}`)는 삭제 후에도 200을 유지한다 — Task의
        "회의록에서 보기" 근거 링크가 회의를 삭제해도 계속 연결돼야 하기
        때문이다(뒤이은 사용자 요청, `include_deleted=True`). 목록
        (`GET /api/v1/meetings`)에서만 사라진다."""

        self.client.post("/api/v1/meetings", json={"meeting_id": "m-delete", "title": "삭제할 회의"})
        response = self.client.delete("/api/v1/meetings/m-delete")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.get("/api/v1/meetings/m-delete").status_code, 200)
        self.assertNotIn("m-delete", [item["meeting_id"] for item in self.client.get("/api/v1/meetings").json()])

    def test_delete_unknown_meeting_returns_404(self):
        self.assertEqual(self.client.delete("/api/v1/meetings/does-not-exist").status_code, 404)

    def test_delete_is_scoped_to_the_owning_user(self):
        self.client.post("/api/v1/meetings", json={"meeting_id": "m-owned-by-a", "title": "user-a 회의"})
        app.dependency_overrides[_authenticated_user_or_assignee] = lambda: "user-b"
        try:
            response = self.client.delete("/api/v1/meetings/m-owned-by-a")
        finally:
            app.dependency_overrides[_authenticated_user_or_assignee] = lambda: "user-a"
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get("/api/v1/meetings/m-owned-by-a").status_code, 200)

    def test_has_analysis_reflects_whether_a_summary_is_saved(self):
        """실사용 중 발견한 버그(2026-08-17)의 회귀 테스트 — Context에 회의별
        분석 여부가 전혀 없어 자연어 Router가 "아직 분석 안 한 회의"라고 근거
        없이 단정했다. `GET /api/v1/meetings`(목록)·`GET /{id}`(상세) 모두
        `summary` 저장 여부로 계산한 `has_analysis`를 실어야 한다."""

        self.client.post("/api/v1/meetings", json={"meeting_id": "m-unanalyzed", "title": "미분석 회의"})
        self.client.post("/api/v1/meetings", json={"meeting_id": "m-analyzed", "title": "분석된 회의"})
        meeting_repository().set_summary("m-analyzed", "user-a", "요약 텍스트")

        listed = {item["meeting_id"]: item["has_analysis"] for item in self.client.get("/api/v1/meetings").json()}
        self.assertFalse(listed["m-unanalyzed"])
        self.assertTrue(listed["m-analyzed"])

        self.assertFalse(self.client.get("/api/v1/meetings/m-unanalyzed").json()["has_analysis"])
        self.assertTrue(self.client.get("/api/v1/meetings/m-analyzed").json()["has_analysis"])


if __name__ == "__main__":
    unittest.main()
