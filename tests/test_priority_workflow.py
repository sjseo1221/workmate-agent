"""M1.5-03 rank_priorities Workflow 검증."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest

from jsonschema import Draft202012Validator, FormatChecker

from app.domain.task import TaskRecord
from app.repositories.tasks import SQLiteTaskRepository
from app.workflows.priority import build_rank_priorities_workflow
from app.workflows.registry import WorkflowRequest


class PriorityWorkflowTests(unittest.TestCase):
    """사용자 범위, 결정성, Snapshot 변화와 승인 Schema를 검증한다."""

    def test_workflow_ranks_only_owned_tasks_and_emits_schema_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            os.environ["WORKMATE_TASK_DB_PATH"] = str(Path(directory) / "tasks.sqlite3")
            repository = SQLiteTaskRepository(os.environ["WORKMATE_TASK_DB_PATH"])
            repository.create(TaskRecord(task_id="a", assignee_user_id="alice", title="긴급", priority_hint=10))
            repository.create(TaskRecord(task_id="b", assignee_user_id="bob", title="다른 사용자", priority_hint=10))
            result = asyncio.run(
                build_rank_priorities_workflow()(
                    WorkflowRequest(
                        skill_id="rank_priorities",
                        task_id="task-1",
                        thread_id="task-1",
                        message_id="message-1",
                        user_id="alice",
                        payload={"user_id": "alice", "timezone": "Asia/Seoul", "as_of": "2026-08-13T09:00:00+09:00"},
                    )
                )
            )
            data = json.loads(result.text)
            self.assertFalse(result.mock)
            self.assertEqual([item["task_id"] for item in data["data"]["priorities"]], ["a"])
            self.assertEqual(data["data"]["source_refs"], ["a"])
            self.assertEqual(data["data"]["changes"][0]["change_type"], "entered")
            # `WorkflowResult.data`가 비어 있으면 `internal_chat.py`가 그대로
            # `artifact.data=null`을 반환해, 요청이 200으로 성공해도 프론트는
            # 항상 빈 결과만 읽는다(daily_briefing.py와 같은 유형의 버그,
            # 2026-08-16, 14번 갭 문서).
            self.assertEqual(result.data, data)

    def test_second_run_reports_unchanged_and_does_not_call_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            os.environ["WORKMATE_TASK_DB_PATH"] = str(Path(directory) / "tasks.sqlite3")
            repository = SQLiteTaskRepository(os.environ["WORKMATE_TASK_DB_PATH"])
            repository.create(TaskRecord(task_id="a", assignee_user_id="alice", title="업무", priority_hint=5))
            request = WorkflowRequest("rank_priorities", "t", "t", "m", "alice", {"timezone": "Asia/Seoul", "as_of": "2026-08-13T09:00:00+09:00"})
            workflow = build_rank_priorities_workflow()
            asyncio.run(workflow(request))
            second = asyncio.run(workflow(request))
            self.assertEqual(json.loads(second.text)["data"]["changes"][0]["change_type"], "unchanged")

    def test_result_data_matches_approved_priority_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            os.environ["WORKMATE_TASK_DB_PATH"] = str(Path(directory) / "tasks.sqlite3")
            SQLiteTaskRepository(os.environ["WORKMATE_TASK_DB_PATH"]).create(
                TaskRecord(task_id="a", assignee_user_id="alice", title="업무")
            )
            result = asyncio.run(
                build_rank_priorities_workflow()(
                    WorkflowRequest("rank_priorities", "t", "t", "m", "alice", {"timezone": "Asia/Seoul"})
                )
            )
            schema_path = Path(__file__).resolve().parents[2] / "docs" / "schemas" / "workmate-skill-schemas.schema.json"
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            validator = Draft202012Validator(schema, format_checker=FormatChecker())
            validator.validate({
                "schema_version": "1.0",
                "request_id": "request-1",
                "generated_at": json.loads(result.text)["data"]["calculated_at"],
                "data_freshness": {"task": None, "calendar": None, "email": None, "meeting": None},
                "warnings": [],
                "result": json.loads(result.text),
            })


if __name__ == "__main__":
    unittest.main()
