"""M0.1-03 영속 Task·멱등성·Checkpoint 경계 테스트."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from a2a.server.context import ServerCallContext
from a2a.types import Artifact, ListTasksRequest, Part, Task, TaskState, TaskStatus

from app.a2a.persistence import PersistentTaskStore
from app.workflows.registry import (
    WorkflowNotImplementedError,
    WorkflowRegistry,
    WorkflowRequest,
    WorkflowResult,
)


class PersistenceBoundaryTests(unittest.TestCase):
    """프로세스 재생성 후에도 A2A 인프라 상태가 남는지 확인한다."""

    def test_task_message_artifact_and_checkpoint_survive_store_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a2a.sqlite3"
            context = ServerCallContext()
            task = Task(
                id="task-1",
                context_id="context-1",
                status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
            )

            first = PersistentTaskStore(path)
            asyncio.run(first.save(task, context))
            self.assertTrue(
                asyncio.run(first.record_message("message-1", "task-1", {"x": 1}))
            )
            self.assertFalse(
                asyncio.run(first.record_message("message-1", "task-1", {"x": 1}))
            )
            asyncio.run(
                first.save_artifact(
                    "task-1",
                    Artifact(
                        artifact_id="artifact-1",
                        name="result",
                        description="test",
                        parts=[Part(text="ok", media_type="text/plain")],
                    ),
                )
            )
            asyncio.run(
                first.save_checkpoint(
                    "task-1",
                    "checkpoint-1",
                    {"step": "working"},
                    {"message_id": "message-1"},
                )
            )

            restarted = PersistentTaskStore(path)
            restored = asyncio.run(restarted.get("task-1", context))
            self.assertIsNotNone(restored)
            self.assertEqual(restored.id, "task-1")
            self.assertEqual(
                asyncio.run(restarted.list(ListTasksRequest(), context)).total_size, 1
            )

    def test_message_id_reuse_with_different_payload_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PersistentTaskStore(Path(directory) / "a2a.sqlite3")
            context = ServerCallContext()
            asyncio.run(
                store.save(
                    Task(
                        id="task-1",
                        context_id="context-1",
                        status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                    ),
                    context,
                )
            )
            asyncio.run(store.record_message("message-1", "task-1", {"x": 1}))
            with self.assertRaises(ValueError):
                asyncio.run(store.record_message("message-1", "task-1", {"x": 2}))


class WorkflowRegistryTests(unittest.TestCase):
    """Registry가 등록된 Workflow만 실행하도록 확인한다."""

    def test_registry_resolves_registered_handler(self) -> None:
        registry = WorkflowRegistry()

        async def handler(request: WorkflowRequest) -> WorkflowResult:
            return WorkflowResult("test", "test", request.skill_id)

        registry.register("test_skill", handler)
        result = asyncio.run(
            registry.execute(
                WorkflowRequest("test_skill", "task", "thread", "message", "user", {})
            )
        )
        self.assertEqual(result.text, "test_skill")

    def test_registry_rejects_unimplemented_workflow(self) -> None:
        with self.assertRaises(WorkflowNotImplementedError):
            WorkflowRegistry().resolve("daily_briefing")


if __name__ == "__main__":
    unittest.main()
