"""M4.4 근거 기반 답변·출처·근거 부족 처리 테스트."""

from __future__ import annotations

import unittest
from uuid import uuid4

from app.meeting_answer import MeetingAnswerService
from app.repositories.meeting_chunks import HybridSearchHit


def hit(*, score: float, content: str = "회의 결정 원문") -> HybridSearchHit:
    """답변 서비스 단위 테스트용 검색 결과를 만든다."""
    chunk_id = str(uuid4())
    return HybridSearchHit(
        meeting_chunk_id=chunk_id,
        meeting_id="meeting-1",
        user_id="user-1",
        content=content,
        meeting_title="예산 회의",
        meeting_started_at=None,
        sequence_no=0,
        rrf_score=score,
        dense_rank=1,
        keyword_rank=1,
    )


class MeetingAnswerTests(unittest.TestCase):
    """출처 누락과 근거 부족을 차단하는지 확인한다."""

    def test_grounded_answer_contains_exact_evidence_and_source(self) -> None:
        result = MeetingAnswerService().answer_from_hits("예산 결정", [hit(score=0.02)])
        self.assertTrue(result.grounded)
        self.assertEqual(len(result.sources), 1)
        self.assertIn(result.sources[0].evidence_text, result.answer)
        self.assertIn(result.sources[0].source_id, result.answer)

    def test_insufficient_evidence_is_explicitly_refused(self) -> None:
        result = MeetingAnswerService().answer_from_hits("무관한 질문", [hit(score=0.001)])
        self.assertFalse(result.grounded)
        self.assertEqual(result.sources, ())
        self.assertTrue(result.warnings)
        self.assertIn("부족", result.answer)

    def test_empty_query_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MeetingAnswerService().answer_from_hits(" ", [])


if __name__ == "__main__":
    unittest.main()
