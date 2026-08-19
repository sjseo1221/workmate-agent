"""M0.4-03 테스트 전용 WORKING Task Fixture 계약 테스트."""

from __future__ import annotations

import asyncio
import unittest

from a2a.server.context import ServerCallContext
from a2a.types import Task, TaskState, TaskStatus

from app.internal_chat import _InternalUser
from tests.ui_working_fixture import WorkingTaskFixtureStore, WorkingWorkflowFixture


class UiWorkingFixtureTests(unittest.TestCase):
    """Fixture의 사용자 격리와 취소 상태 전환을 검증한다."""

    def test_working_task_can_be_cancelled_and_is_user_scoped(self) -> None:
        async def scenario() -> None:
            store = WorkingTaskFixtureStore()
            owner = _InternalUser("fixture-user")
            other = _InternalUser("other-user")
            task = Task(
                id="fixture-task",
                context_id="fixture-task",
                status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
            )
            await store.save(task, ServerCallContext(user=owner))
            self.assertIsNone(await store.get(task.id, ServerCallContext(user=other)))
            self.assertEqual(
                (await store.get(task.id, ServerCallContext(user=owner))).status.state,
                TaskState.TASK_STATE_WORKING,
            )
            await store.mark_cancel_requested(task.id)
            self.assertEqual(
                (await store.get(task.id, ServerCallContext(user=owner))).status.state,
                TaskState.TASK_STATE_CANCELED,
            )

        asyncio.run(scenario())

    def test_workflow_fixture_returns_working_result(self) -> None:
        async def scenario() -> None:
            result = await WorkingWorkflowFixture().execute(None)
            self.assertEqual(result.state, "working")
            self.assertTrue(result.mock)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
