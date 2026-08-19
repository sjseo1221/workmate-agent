"""회의 검색 결과를 근거 기반 답변과 출처로 변환한다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections.abc import Sequence

from app.repositories.meeting_chunks import HybridSearchHit


@dataclass(frozen=True, slots=True)
class MeetingSource:
    """답변에 표시할 회의 Chunk 출처."""

    source_id: str
    meeting_id: str
    meeting_title: str
    meeting_started_at: datetime | None
    meeting_chunk_id: str
    evidence_text: str


@dataclass(frozen=True, slots=True)
class MeetingAnswer:
    """검색 근거와 부족 여부를 포함한 답변 결과."""

    query: str
    answer: str
    sources: tuple[MeetingSource, ...]
    grounded: bool
    warnings: tuple[str, ...] = ()


class MeetingAnswerService:
    """Hybrid 검색 결과만 사용해 환각 없는 회의 답변을 만든다."""

    def answer_from_hits(
        self,
        query: str,
        hits: Sequence[HybridSearchHit],
        *,
        max_sources: int = 5,
        minimum_rrf_score: float = 0.01,
    ) -> MeetingAnswer:
        """검색 결과를 출처와 함께 반환하고 근거가 약하면 답변을 거절한다."""
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("query must not be empty")
        if max_sources < 1 or max_sources > 20:
            raise ValueError("max_sources must be between 1 and 20")
        selected = [hit for hit in hits if hit.rrf_score >= minimum_rrf_score][:max_sources]
        if not selected:
            return MeetingAnswer(
                query=normalized_query,
                answer="회의 근거가 부족해 답변을 생성할 수 없습니다.",
                sources=(),
                grounded=False,
                warnings=("insufficient meeting evidence",),
            )
        sources = tuple(
            MeetingSource(
                source_id=f"meeting-chunk:{hit.meeting_chunk_id}",
                meeting_id=hit.meeting_id,
                meeting_title=hit.meeting_title,
                meeting_started_at=hit.meeting_started_at,
                meeting_chunk_id=hit.meeting_chunk_id,
                evidence_text=hit.content,
            )
            for hit in selected
        )
        lines = [f"'{normalized_query}'에 대해 검색된 회의 근거입니다."]
        lines.extend(
            f"- {source.meeting_title}: {source.evidence_text} "
            f"[출처: {source.source_id}]"
            for source in sources
        )
        return MeetingAnswer(
            query=normalized_query,
            answer="\n".join(lines),
            sources=sources,
            grounded=True,
        )
