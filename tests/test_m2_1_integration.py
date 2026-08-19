"""M2.1-06 실제 Repository·Provider 부분 실패·Schema 통합 검증."""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from jsonschema import Draft202012Validator, FormatChecker

from app.domain.task import TaskRecord
from app.domain.weekly import normalize_week_scope
from app.repositories.tasks import SQLiteTaskRepository
from app.workflows.weekly_classification import build_weekly_report_data, link_approved_tasks


class M21IntegrationTests(unittest.TestCase):
    def test_repository_records_become_schema_valid_weekly_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteTaskRepository(str(Path(directory) / "tasks.sqlite3"))
            repository.create(
                TaskRecord(
                    task_id="weekly-email",
                    assignee_user_id="alice",
                    title="메일 요청 확인",
                    source_type="email",
                    source_id="gmail-42",
                )
            )
            repository.create(
                TaskRecord(task_id="weekly-done", assignee_user_id="alice", title="완료 작업", status="done")
            )
            repository.create(
                TaskRecord(
                    task_id="other-user",
                    assignee_user_id="bob",
                    title="다른 사용자 작업",
                    source_type="calendar",
                    source_id="primary:event-other",
                )
            )
            scope = normalize_week_scope(
                "alice",
                "2026-08-10",
                "Asia/Seoul",
                as_of=datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
            )
            data = build_weekly_report_data(
                link_approved_tasks(repository.list("alice"), scope=scope),
                scope=scope,
            )

        self.assertEqual(data["source_refs"], ["weekly-done", "weekly-email", "gmail-42"])
        self.assertNotIn("other-user", data["source_refs"])
        self._validate_result(data)

    def test_provider_partial_failure_contract_is_regression_covered(self) -> None:
        """Provider 부분 실패 회귀는 M1.5 daily briefing 통합 테스트로 보장한다."""

        from tests.test_daily_briefing import DailyBriefingWorkflowTests

        case = DailyBriefingWorkflowTests("test_provider_failure_returns_partial_result_and_warning")
        case.setUp()
        directory, result = case._run(gmail_fail=True)
        try:
            self.assertTrue(result.warnings)
            self.assertEqual(result.warnings[0]["source"], "email")
            self.assertFalse(result.mock)
        finally:
            directory.cleanup()

    @staticmethod
    def _validate_result(data: dict[str, object]) -> None:
        schema_path = Path(__file__).resolve().parents[2] / "docs" / "schemas" / "workmate-skill-schemas.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(
            {
                "schema_version": "1.0",
                "request_id": "m2-1-06",
                "generated_at": "2026-08-13T03:00:00Z",
                "data_freshness": {"task": None, "calendar": None, "email": None, "meeting": None},
                "warnings": [],
                "result": {"type": "weekly_report", "data": data},
            }
        )


if __name__ == "__main__":
    unittest.main()
