"""M0.1-03 PostgreSQL 실제 연결 경계 검증.

`DATABASE_URL`이 설정된 실행에서만 수행하며, 로컬 기본 테스트에서는 건너뛴다.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest

from a2a.server.context import ServerCallContext
from a2a.types import ListTasksRequest, Task, TaskState, TaskStatus

from app.a2a.persistence import PostgresTaskStore


@unittest.skipUnless(os.getenv("DATABASE_URL"), "DATABASE_URL is not configured")
class PostgresPersistenceIntegrationTests(unittest.TestCase):
    """PostgreSQL Migration과 Task·Message 멱등성 경계를 확인한다."""

    def test_migration_task_snapshot_and_message_idempotency(self) -> None:
        # Windows ProactorEventLoop는 psycopg async 연결을 지원하지 않으므로
        # 실제 PostgreSQL 통합 테스트에서만 SelectorEventLoop를 사용한다.
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        async def scenario() -> None:
            store = PostgresTaskStore(os.environ["DATABASE_URL"])
            context = ServerCallContext()
            task = Task(
                id="postgres-integration-task",
                context_id="postgres-integration-context",
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            )

            self.assertTrue(await store.check_ready())
            await store.save(task, context)
            self.assertTrue(
                await store.record_message(
                    "postgres-integration-message",
                    task.id,
                    {"skill_id": "runtime_bootstrap", "request": "integration"},
                )
            )
            self.assertFalse(
                await store.record_message(
                    "postgres-integration-message",
                    task.id,
                    {"skill_id": "runtime_bootstrap", "request": "integration"},
                )
            )
            restored = await store.get(task.id, context)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.id, task.id)
            listed = await store.list(ListTasksRequest(), context)
            self.assertTrue(any(item.id == task.id for item in listed.tasks))
            await store.delete(task.id, context)
            self.assertIsNone(await store.get(task.id, context))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
