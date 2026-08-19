"""M3.4 분석 결과 Schema와 근거 검증 테스트."""

import unittest

from app.meeting_analysis import MeetingAnalysisProviderError, MeetingAnalysis, analyze_transcript


class MeetingAnalysisTests(unittest.TestCase):
    def test_schema_and_evidence(self):
        result = MeetingAnalysis.model_validate({"title": "QA 배포 일정 점검 회의", "summary": "QA 배포 일정을 확인했다.", "action_items": [{"action_item_id": "a1", "title": "QA 결과 공유", "assignee_id": "u1", "due_at": None, "evidence_text": "오늘 오후까지 테스트팀에서 결과를 공유", "start_ms": 1000, "end_ms": 2000}]})
        self.assertEqual(result.action_items[0].evidence_text, "오늘 오후까지 테스트팀에서 결과를 공유")

    def test_provider_requires_key(self):
        with self.assertRaises(MeetingAnalysisProviderError):
            analyze_transcript("회의 내용", api_key=None, base_url="https://example.invalid")


if __name__ == "__main__":
    unittest.main()
