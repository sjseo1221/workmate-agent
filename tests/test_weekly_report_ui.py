"""M2.4 주간 보고서 UI 정적 계약 테스트."""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.main import app


class WeeklyReportUiTests(unittest.TestCase):
    """화면의 필수 입력·섹션·복사 제어를 확인한다."""

    def test_page_contains_report_controls_and_sections(self) -> None:
        """기간 선택, 생성, Markdown 복사와 다섯 섹션이 화면에 존재해야 한다."""

        response = TestClient(app).get("/weekly-report")
        self.assertEqual(response.status_code, 200)
        body = response.text
        for text in ("OIDC Bearer Token", "기준 주", "보고서 생성", "Markdown 복사", "완료", "진행", "지연", "미해결 이슈", "다음 주 계획"):
            self.assertIn(text, body)


if __name__ == "__main__":
    unittest.main()
