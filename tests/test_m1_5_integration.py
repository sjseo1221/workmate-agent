"""M1.5-05 PostgreSQL·Workflow·Contract 통합 검수."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from app.domain.task import TaskRecord
from app.repositories.priority import PostgresPrioritySnapshotRepository
from app.repositories.sync_state import PostgresSyncStateRepository
from app.repositories.tasks import PostgresTaskRepository
from app.workflows.daily_briefing import build_daily_briefing_workflow
from app.workflows.priority import build_rank_priorities_workflow
from app.workflows.registry import WorkflowRequest

TEST_DATABASE_URL = os.getenv("WORKMATE_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_DATABASE_URL, "WORKMATE_TEST_DATABASE_URL is not configured")
class M15PostgresIntegrationTests(unittest.TestCase):
    """M1.5 업무 Workflow가 운영 PostgreSQL 저장 경계와 연결되는지 확인한다."""

    def test_rank_priorities_persists_postgres_snapshot_and_is_user_scoped(self) -> None:
        dsn = TEST_DATABASE_URL
        tasks = PostgresTaskRepository(dsn)
        snapshots = PostgresPrioritySnapshotRepository(dsn)
        owner = str(uuid4())
        other = str(uuid4())
        tasks.create(TaskRecord(task_id=str(uuid4()), assignee_user_id=owner, title="통합 우선순위", priority_hint=10))
        tasks.create(TaskRecord(task_id=str(uuid4()), assignee_user_id=other, title="타 사용자 Task", priority_hint=10))
        with patch.dict(os.environ, {"DATABASE_URL": dsn}):
            workflow = build_rank_priorities_workflow()
        result = asyncio.run(
            workflow(
                WorkflowRequest(
                    "rank_priorities", str(uuid4()), str(uuid4()), str(uuid4()), owner,
                    {"user_id": owner, "timezone": "Asia/Seoul", "as_of": "2026-08-13T09:00:00+09:00"},
                )
            )
        )
        payload = json.loads(result.text)
        self.assertFalse(result.mock)
        self.assertEqual(len(payload["data"]["priorities"]), 1)
        self.assertEqual(len(snapshots.latest(owner)), 1)
        self.assertEqual(snapshots.latest(other), [])

    def test_daily_briefing_uses_postgres_task_and_sync_boundaries(self) -> None:
        dsn = TEST_DATABASE_URL
        tasks = PostgresTaskRepository(dsn)
        sync = PostgresSyncStateRepository(dsn)
        owner = str(uuid4())
        tasks.create(TaskRecord(task_id=str(uuid4()), assignee_user_id=owner, title="브리핑 Task"))

        class GmailFixture:
            def list_messages(self, **kwargs):
                from app.providers.google import GmailMessage
                return [GmailMessage("integration-message", "thread", "검토", ("INBOX",))]

        class CalendarFixture:
            def list_events(self, **kwargs):
                return [], "integration-calendar-token"

        result = asyncio.run(
            build_daily_briefing_workflow(
                provider_factory=lambda: (GmailFixture(), CalendarFixture()),
                repositories=(tasks, sync),
            )(
                WorkflowRequest(
                    "daily_briefing", str(uuid4()), str(uuid4()), str(uuid4()), owner,
                    {"user_id": owner, "timezone": "Asia/Seoul", "as_of": "2026-08-13T09:00:00+09:00"},
                )
            )
        )
        payload = json.loads(result.text)
        self.assertFalse(result.mock)
        self.assertEqual(payload["type"], "daily_briefing")
        self.assertEqual(len(payload["data"]["important_signals"]), 1)
        self.assertEqual(payload["data"]["calendar_events"], [])


if __name__ == "__main__":
    unittest.main()
