"""M3.5 Action Item 승인·Task 멱등성 테스트."""

import os
import tempfile
import unittest
from fastapi.testclient import TestClient

from app.internal_chat import _authenticated_user_or_assignee
from app.main import app
from app.meeting_api import meeting_repository
from app.task_api import task_repository


class ActionItemApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["WORKMATE_MEETING_DB_PATH"] = os.path.join(cls.tmp.name, "meetings.sqlite3")
        os.environ["WORKMATE_TASK_DB_PATH"] = os.path.join(cls.tmp.name, "tasks.sqlite3")
        meeting_repository.cache_clear(); task_repository.cache_clear()
        app.dependency_overrides[_authenticated_user_or_assignee] = lambda: "user-a"
        cls.client = TestClient(app)
        cls.client.post("/api/v1/meetings", json={"meeting_id": "m-action", "title": "분석 회의"})

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(_authenticated_user_or_assignee, None)
        meeting_repository.cache_clear(); task_repository.cache_clear()
        os.environ.pop("WORKMATE_MEETING_DB_PATH", None); os.environ.pop("WORKMATE_TASK_DB_PATH", None)
        cls.tmp.cleanup()

    def test_approval_is_idempotent(self):
        payload = {"title": "QA 결과 공유", "evidence_text": "오늘 오후까지 결과를 공유"}
        first = self.client.post("/api/v1/meetings/m-action/actions/a-1/approve", json=payload)
        second = self.client.post("/api/v1/meetings/m-action/actions/a-1/approve", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["task_id"], second.json()["task_id"])
        self.assertEqual(len(self.client.get("/api/v1/tasks").json()), 1)

    def test_same_llm_assigned_action_item_id_does_not_collide_across_meetings(self):
        """`action_item_id`는 LLM이 스스로 매기는 값이라 회의마다 겹칠 수
        있다(`temperature=0`으로 비슷한 회의록에 반복 분석 시 특히) — 예전엔
        `action_item_id` 하나만 PRIMARY KEY라 다른 회의의 같은 ID가 조용히
        무시돼 `AssertionError` → 500이 났다(2026-08-15 실사용 중 발견)."""

        self.client.post("/api/v1/meetings", json={"meeting_id": "m-action-2", "title": "다른 분석 회의"})
        payload_first = {"title": "QA 결과 공유", "evidence_text": "오늘 오후까지 결과를 공유"}
        payload_second = {"title": "배포 승인 요청", "evidence_text": "내일까지 배포를 승인"}
        first = self.client.post("/api/v1/meetings/m-action/actions/a-collide/approve", json=payload_first)
        second = self.client.post("/api/v1/meetings/m-action-2/actions/a-collide/approve", json=payload_second)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(first.json()["task_id"], second.json()["task_id"])

    def test_approve_source_id_embeds_meeting_id_for_evidence_lookup(self):
        """Task의 `source_id`가 `{meeting_id}:{action_item_id}` 형태여야 Task 상세에서
        어느 회의의 근거인지 되짚을 수 있다(2026-08-16, 17번 갭 문서 #11)."""

        self.client.post("/api/v1/meetings", json={"meeting_id": "m-source-id", "title": "근거 회의"})
        payload = {"title": "제목", "evidence_text": "근거"}
        self.client.post("/api/v1/meetings/m-source-id/actions/a-src/approve", json=payload)
        [task] = [t for t in self.client.get("/api/v1/tasks").json() if t["source_id"] == "m-source-id:a-src"]
        self.assertEqual(task["source_type"], "action_item")

    def test_approved_task_exposes_read_only_meeting_evidence(self):
        """승인된 Action Item Task의 상세에는 회의 근거(`evidence_text` 등)가
        읽기 전용으로 붙어야 한다(2026-08-16, 17번 갭 문서 #11)."""

        self.client.post("/api/v1/meetings", json={"meeting_id": "m-evidence", "title": "근거 노출 회의"})
        payload = {"title": "근거 확인용 제목", "evidence_text": "다음 주까지 근거를 확인한다"}
        approved = self.client.post("/api/v1/meetings/m-evidence/actions/a-ev/approve", json=payload)
        task_id = approved.json()["task_id"]

        task = self.client.get(f"/api/v1/tasks/{task_id}")
        self.assertEqual(task.status_code, 200)
        evidence = task.json()["meeting_evidence"]
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["meeting_id"], "m-evidence")
        self.assertEqual(evidence["meeting_title"], "근거 노출 회의")
        self.assertEqual(evidence["action_item_id"], "a-ev")
        self.assertEqual(evidence["evidence_text"], "다음 주까지 근거를 확인한다")

    def test_manual_task_has_no_meeting_evidence(self):
        """수동으로 만든 Task는 `meeting_evidence`가 `null`이어야 한다."""

        created = self.client.post("/api/v1/tasks", json={"title": "수동 작업"})
        self.assertIsNone(created.json()["meeting_evidence"])


class MeetingAnalysisApiTests(unittest.TestCase):
    """`GET .../analysis` 재조회 계약 테스트(2026-08-16, 17번 갭 문서 #10)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["WORKMATE_MEETING_DB_PATH"] = os.path.join(cls.tmp.name, "meetings.sqlite3")
        os.environ["WORKMATE_TASK_DB_PATH"] = os.path.join(cls.tmp.name, "tasks.sqlite3")
        meeting_repository.cache_clear(); task_repository.cache_clear()
        app.dependency_overrides[_authenticated_user_or_assignee] = lambda: "user-a"
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(_authenticated_user_or_assignee, None)
        meeting_repository.cache_clear(); task_repository.cache_clear()
        os.environ.pop("WORKMATE_MEETING_DB_PATH", None); os.environ.pop("WORKMATE_TASK_DB_PATH", None)
        cls.tmp.cleanup()

    def test_analysis_returns_404_before_analyze_meeting_ever_ran(self):
        self.client.post("/api/v1/meetings", json={"meeting_id": "m-unanalyzed", "title": "아직 미분석"})
        response = self.client.get("/api/v1/meetings/m-unanalyzed/analysis")
        self.assertEqual(response.status_code, 404)

    def test_analysis_returns_404_for_missing_meeting(self):
        response = self.client.get("/api/v1/meetings/missing-meeting/analysis")
        self.assertEqual(response.status_code, 404)

    def test_analysis_returns_persisted_summary_and_action_items(self):
        self.client.post("/api/v1/meetings", json={"meeting_id": "m-analyzed", "title": "분석 완료"})
        meeting_repository().set_summary("m-analyzed", "user-a", "이번 회의는 QA 결과 공유가 핵심이었다.")
        meeting_repository().upsert_action(
            "a-1", "m-analyzed", "user-a", "QA 결과 공유", "오늘 오후까지 결과를 공유",
            due_at="2026-08-20T00:00:00+00:00", assignee_user_id="user-a",
            start_ms=1000, end_ms=2000, meeting_chunk_id="chunk-1",
        )
        response = self.client.get("/api/v1/meetings/m-analyzed/analysis")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["summary"], "이번 회의는 QA 결과 공유가 핵심이었다.")
        self.assertEqual(len(body["action_items"]), 1)
        item = body["action_items"][0]
        self.assertEqual(item["action_item_id"], "a-1")
        self.assertEqual(item["assignee_user_id"], "user-a")
        self.assertEqual(item["due_at"], "2026-08-20T00:00:00+00:00")
        self.assertEqual(item["evidence_span"]["meeting_chunk_ids"], ["chunk-1"])
        self.assertEqual(item["evidence_span"]["start_ms"], 1000)
        self.assertEqual(item["approval_status"], "pending")

    def test_analysis_still_available_after_the_meeting_is_deleted(self):
        """사용자 요청(2026-08-17)의 회귀 테스트 — "회의록에서 보기"로 들어오면
        삭제된 회의라도 저장된 분석 결과를 계속 볼 수 있어야 한다."""

        self.client.post("/api/v1/meetings", json={"meeting_id": "m-deleted-analyzed", "title": "삭제될 분석 완료 회의"})
        meeting_repository().set_summary("m-deleted-analyzed", "user-a", "요약문")
        self.assertEqual(self.client.delete("/api/v1/meetings/m-deleted-analyzed").status_code, 204)
        response = self.client.get("/api/v1/meetings/m-deleted-analyzed/analysis")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"], "요약문")


class ActionsReviewBatchApiTests(unittest.TestCase):
    """`POST .../actions:review` 배치 검토 계약 테스트(2026-08-16, 17번 갭 문서 #8·#9)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["WORKMATE_MEETING_DB_PATH"] = os.path.join(cls.tmp.name, "meetings.sqlite3")
        os.environ["WORKMATE_TASK_DB_PATH"] = os.path.join(cls.tmp.name, "tasks.sqlite3")
        meeting_repository.cache_clear(); task_repository.cache_clear()
        app.dependency_overrides[_authenticated_user_or_assignee] = lambda: "user-a"
        cls.client = TestClient(app)
        cls.client.post("/api/v1/meetings", json={"meeting_id": "m-review", "title": "배치 검토 회의"})

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(_authenticated_user_or_assignee, None)
        meeting_repository.cache_clear(); task_repository.cache_clear()
        os.environ.pop("WORKMATE_MEETING_DB_PATH", None); os.environ.pop("WORKMATE_TASK_DB_PATH", None)
        cls.tmp.cleanup()

    def setUp(self):
        meeting_repository().upsert_action("a-approve", "m-review", "user-a", "승인될 항목", "근거1")
        meeting_repository().upsert_action("a-edit", "m-review", "user-a", "수정될 항목", "근거2")
        meeting_repository().upsert_action("a-reject", "m-review", "user-a", "거절될 항목", "근거3")

    def test_batch_review_dispatches_approve_edit_reject_in_one_call(self):
        response = self.client.post("/api/v1/meetings/m-review/actions:review", json={
            "decisions": [
                {"action_item_id": "a-approve", "decision": "approve"},
                {"action_item_id": "a-edit", "decision": "edit", "changes": {"title": "고친 제목"}},
                {"action_item_id": "a-reject", "decision": "reject"},
            ]
        })
        self.assertEqual(response.status_code, 200, response.text)
        results = {r["action_item_id"]: r for r in response.json()["results"]}
        self.assertEqual(results["a-approve"]["approval_status"], "approved")
        self.assertIsNotNone(results["a-approve"]["task_id"])
        self.assertEqual(results["a-edit"]["approval_status"], "pending")
        self.assertEqual(results["a-reject"]["approval_status"], "rejected")

        analysis = self.client.get("/api/v1/meetings/m-review/analysis").json()
        edited = next(a for a in analysis["action_items"] if a["action_item_id"] == "a-edit")
        self.assertEqual(edited["title"], "고친 제목")

    def test_changes_with_non_edit_decision_is_rejected(self):
        response = self.client.post("/api/v1/meetings/m-review/actions:review", json={
            "decisions": [{"action_item_id": "a-approve", "decision": "approve", "changes": {"title": "안됨"}}]
        })
        self.assertEqual(response.status_code, 422)

    def test_unknown_action_item_id_is_404(self):
        response = self.client.post("/api/v1/meetings/m-review/actions:review", json={
            "decisions": [{"action_item_id": "does-not-exist", "decision": "reject"}]
        })
        self.assertEqual(response.status_code, 404)

    def test_missing_meeting_is_404(self):
        response = self.client.post("/api/v1/meetings/missing/actions:review", json={
            "decisions": [{"action_item_id": "a-approve", "decision": "reject"}]
        })
        self.assertEqual(response.status_code, 404)

    def test_meeting_evidence_survives_deleting_the_source_meeting(self):
        """사용자 요청(2026-08-17)의 회귀 테스트 — 회의를 삭제해도 그 회의에서 만든
        Task의 "회의록에서 보기" 근거 링크(`meeting_evidence`)는 계속 연결돼야
        한다. 회의 삭제 기능을 추가하며 `_meeting_evidence()`가 조용히 `None`을
        반환하는 회귀가 생겨(`get()`이 기본적으로 삭제된 회의를 숨기게 바뀜),
        `include_deleted=True`로 고쳤다."""

        self.client.post("/api/v1/meetings", json={"meeting_id": "m-evidence", "title": "근거용 회의"})
        meeting_repository().upsert_action("a-evidence", "m-evidence", "user-a", "근거용 항목", "근거 원문")
        review = self.client.post("/api/v1/meetings/m-evidence/actions:review", json={
            "decisions": [{"action_item_id": "a-evidence", "decision": "approve"}]
        })
        task_id = review.json()["results"][0]["task_id"]

        before = self.client.get(f"/api/v1/tasks/{task_id}").json()
        self.assertIsNotNone(before["meeting_evidence"])
        self.assertEqual(before["meeting_evidence"]["meeting_id"], "m-evidence")

        self.assertEqual(self.client.delete("/api/v1/meetings/m-evidence").status_code, 204)

        after = self.client.get(f"/api/v1/tasks/{task_id}").json()
        self.assertIsNotNone(after["meeting_evidence"])
        self.assertEqual(after["meeting_evidence"]["evidence_text"], "근거 원문")
        # 삭제된 회의라도 상세 조회는 여전히 가능해야 "회의록에서 보기"가 실제로 연결된다.
        self.assertEqual(self.client.get("/api/v1/meetings/m-evidence").status_code, 200)
        # 목록에는 더는 안 보인다.
        self.assertNotIn("m-evidence", [m["meeting_id"] for m in self.client.get("/api/v1/meetings").json()])


if __name__ == "__main__":
    unittest.main()
