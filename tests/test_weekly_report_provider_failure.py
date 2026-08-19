"""M2.3-03 주간 보고서 Provider 재시도와 부분 실패 테스트."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.domain.task import TaskRecord
from app.repositories.tasks import SQLiteTaskRepository
from app.workflows.registry import WorkflowRequest
from app.workflows.weekly_report import LLMProviderError, build_weekly_report_workflow


def _fixture() -> tuple[tempfile.TemporaryDirectory[str], SQLiteTaskRepository, WorkflowRequest]:
    """Provider 경계 테스트에 필요한 최소 Task 원장과 요청을 만든다."""

    directory = tempfile.TemporaryDirectory()
    repository = SQLiteTaskRepository(str(Path(directory.name) / "tasks.sqlite3"))
    repository.create(TaskRecord(task_id="task-done", assignee_user_id="alice", title="배포 자동화", status="done"))
    repository.create(
        TaskRecord(
            task_id="task-next",
            assignee_user_id="alice",
            title="다음 시즌 데이터 반영",
            due_at=datetime(2026, 8, 18, 18, 0, tzinfo=timezone.utc),
        )
    )
    request = WorkflowRequest(
        skill_id="weekly_report",
        task_id="task-run",
        thread_id="thread-run",
        message_id="message-run",
        user_id="alice",
        payload={"timezone": "Asia/Seoul", "week_of": "2026-08-10"},
    )
    return directory, repository, request


class WeeklyReportProviderFailureTests(unittest.TestCase):
    """일시 오류 재시도와 최종 실패의 안전한 부분 결과를 검증한다."""

    def test_transient_failure_retries_once_then_succeeds(self) -> None:
        """첫 Provider 실패 뒤 한 번만 재시도하고 성공 결과를 사용한다."""

        calls = 0

        def client(_payload: dict[str, object]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise LLMProviderError("LLM provider returned HTTP 429")
            return {"summary": "재시도 후 생성된 요약"}

        directory, repository, request = _fixture()
        try:
            with patch("app.workflows.weekly_report.time.sleep") as sleep:
                result = asyncio.run(
                    build_weekly_report_workflow(
                        request, task_repository=repository, llm_client=client
                    )
                )
        finally:
            directory.cleanup()

        self.assertEqual(calls, 2)
        sleep.assert_called_once_with(0.1)
        self.assertEqual(result.data["data"]["summary"], "재시도 후 생성된 요약")
        self.assertEqual(result.warnings, [])

    def test_exhausted_retry_preserves_classification_and_returns_warning(self) -> None:
        """두 번 모두 실패해도 원장 분류를 보존하고 재시도 가능 경고를 반환한다."""

        calls = 0

        def client(_payload: dict[str, object]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            raise LLMProviderError("LLM provider connection failed")

        directory, repository, request = _fixture()
        try:
            with patch("app.workflows.weekly_report.time.sleep"):
                result = asyncio.run(
                    build_weekly_report_workflow(
                        request, task_repository=repository, llm_client=client
                    )
                )
        finally:
            directory.cleanup()

        self.assertEqual(calls, 2)
        self.assertFalse(result.mock)
        self.assertTrue(result.data["data"]["completed"])
        warning = next(item for item in result.warnings if item["source"] == "llm")
        self.assertEqual(warning["code"], "LLM_PROVIDER_UNAVAILABLE")
        self.assertTrue(warning["retryable"])


if __name__ == "__main__":
    unittest.main()
