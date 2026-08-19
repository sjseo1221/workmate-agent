"""M2.3-01 주간 보고서 입력 조립·LLM 생성 Workflow 검증."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import tempfile
import unittest
from pathlib import Path

from app.domain.meeting import MeetingRecord
from app.domain.task import TaskRecord
from app.repositories.meetings import SQLiteMeetingRepository
from app.repositories.tasks import SQLiteTaskRepository
from app.workflows.registry import WorkflowRequest
from app.workflows.weekly_report import LLMProviderError, build_weekly_report_workflow


class WeeklyReportGenerationTests(unittest.TestCase):
    """분류 결과와 LLM 요약의 책임 경계를 확인한다."""

    def _repository(self) -> tuple[tempfile.TemporaryDirectory[str], SQLiteTaskRepository]:
        directory = tempfile.TemporaryDirectory()
        repository = SQLiteTaskRepository(str(Path(directory.name) / "tasks.sqlite3"))
        repository.create(
            TaskRecord(
                task_id="task-done",
                assignee_user_id="alice",
                title="배포 자동화",
                status="done",
            )
        )
        repository.create(
            TaskRecord(
                task_id="task-next",
                assignee_user_id="alice",
                title="다음 시즌 데이터 반영",
                due_at=datetime(2026, 8, 18, 18, 0, tzinfo=timezone.utc),
            )
        )
        return directory, repository

    @staticmethod
    def _request() -> WorkflowRequest:
        return WorkflowRequest(
            skill_id="weekly_report",
            task_id="task-run",
            thread_id="thread-run",
            message_id="message-run",
            user_id="alice",
            payload={
                "schema_version": "1.0",
                "skill_id": "weekly_report",
                "user_id": "alice",
                "timezone": "Asia/Seoul",
                "week_of": "2026-08-10",
            },
        )

    def test_preserves_deterministic_data_and_uses_llm_summary(self) -> None:
        directory, repository = self._repository()
        try:
            async def llm(_: dict[str, object]) -> dict[str, str]:
                return {"summary": "완료 작업과 다음 주 반영 계획을 정리했습니다."}

            result = asyncio.run(
                build_weekly_report_workflow(
                    self._request(), task_repository=repository, llm_client=llm
                )
            )
            payload = json.loads(result.text)
            data = payload["data"]
            self.assertFalse(result.mock)
            self.assertEqual(data["summary"], "완료 작업과 다음 주 반영 계획을 정리했습니다.")
            self.assertEqual(data["completed"][0]["task_id"], "task-done")
            self.assertEqual(data["next_week_plans"][0]["source_refs"], ["task-next"])
            self.assertIn("task-done", data["source_refs"])
            self.assertIn("task-next", data["source_refs"])
            self.assertEqual(result.warnings, [])
        finally:
            directory.cleanup()

    def test_meeting_highlights_reuse_stored_summary_without_a_second_llm_call(self) -> None:
        """"주요 회의 내용"은 `analyze_meeting`이 이미 저장한 `Meeting.summary`를
        그대로 재사용해야 한다 — 새 LLM 호출을 하지 않는다(2026-08-16, 17번 갭
        문서 #17). `WeeklyReportResult`엔 구조화 필드가 없어 Markdown에서만
        확인한다."""

        directory, repository = self._repository()
        meetings = SQLiteMeetingRepository(":memory:")
        try:
            meetings.create(MeetingRecord(
                meeting_id="m-in-week", user_id="alice", title="스프린트 리뷰",
                started_at=datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc),
            ))
            meetings.set_summary("m-in-week", "alice", "다음 빌드 일정에 합의했다.")
            # 주간 범위 밖 회의 — 요약이 있어도 이번 주 보고서에 섞이면 안 된다.
            meetings.create(MeetingRecord(
                meeting_id="m-out-of-week", user_id="alice", title="이전 스프린트 회고",
                started_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
            ))
            meetings.set_summary("m-out-of-week", "alice", "지난 스프린트를 돌아봤다.")
            # 아직 분석하지 않은 회의(summary 없음) — 근거가 없어 제외돼야 한다.
            meetings.create(MeetingRecord(
                meeting_id="m-unanalyzed", user_id="alice", title="분석 전 회의",
                started_at=datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc),
            ))

            async def llm(_: dict[str, object]) -> dict[str, str]:
                return {"summary": "요약"}

            result = asyncio.run(
                build_weekly_report_workflow(
                    self._request(), task_repository=repository, meeting_repository=meetings, llm_client=llm,
                )
            )
            payload = json.loads(result.text)
            # 구조화 JSON엔 없다 — Markdown 본문에만 있다.
            self.assertNotIn("meeting_highlights", payload["data"])
            self.assertIn("## 주요 회의 내용", result.markdown)
            self.assertIn("스프린트 리뷰", result.markdown)
            self.assertIn("다음 빌드 일정에 합의했다.", result.markdown)
            self.assertIn("m-in-week", result.markdown)
            self.assertNotIn("이전 스프린트 회고", result.markdown)
            self.assertNotIn("분석 전 회의", result.markdown)
        finally:
            directory.cleanup()

    def test_provider_failure_returns_source_backed_partial_result(self) -> None:
        directory, repository = self._repository()
        try:
            def failed(_: dict[str, object]) -> dict[str, str]:
                raise LLMProviderError("provider unavailable")

            result = asyncio.run(
                build_weekly_report_workflow(
                    self._request(), task_repository=repository, llm_client=failed
                )
            )
            payload = json.loads(result.text)
            self.assertFalse(result.mock)
            self.assertTrue(result.warnings)
            self.assertEqual(result.warnings[0]["code"], "LLM_PROVIDER_UNAVAILABLE")
            self.assertIn("주간 Task", payload["data"]["summary"])
            self.assertEqual(payload["data"]["next_week_plans"][0]["source_refs"], ["task-next"])
        finally:
            directory.cleanup()


if __name__ == "__main__":
    unittest.main()
