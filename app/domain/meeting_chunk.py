"""회의 검색 Chunk Domain 모델."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections.abc import Sequence


@dataclass(frozen=True, slots=True)
class MeetingChunkRecord:
    """사용자 범위와 1536차원 Embedding을 포함한 검색 Chunk."""

    meeting_chunk_id: str
    parent_meeting_id: str
    user_id: str
    sequence_no: int
    content: str
    embedding: tuple[float, ...]
    speaker: str | None = None
    started_at_ms: int | None = None
    ended_at_ms: int | None = None
    embedding_model: str = "openai/text-embedding-3-small"
    embedding_version: str = "1"
    created_at: datetime | None = None
    # 이 회의가 속한 `meetings` 행의 실제 제목·시각 — PostgreSQL Adapter가 Chunk를 저장할 때
    # 함께 쓰는 부모 `meetings` 행을 진짜 값으로 채우기 위한 것으로, Chunk 자신의 속성이
    # 아니다(검색 결과 인용에 필요한 `meeting_started_at`가 항상 NULL이던 버그 수정,
    # 2026-08-16, 14번 갭 문서 참고).
    meeting_title: str | None = None
    meeting_started_at: datetime | None = None
    meeting_ended_at: datetime | None = None

    def __post_init__(self) -> None:
        """Chunk의 저장 전 필수 필드와 시간·차원 불변식을 검증한다."""

        if not self.content.strip():
            raise ValueError("content must not be empty")
        if self.sequence_no < 0:
            raise ValueError("sequence_no must be non-negative")
        if len(self.embedding) != 1536:
            raise ValueError("embedding must have exactly 1536 dimensions")
        if self.started_at_ms is not None and self.started_at_ms < 0:
            raise ValueError("started_at_ms must be non-negative")
        if self.ended_at_ms is not None and self.ended_at_ms < 0:
            raise ValueError("ended_at_ms must be non-negative")
        if (
            self.started_at_ms is not None
            and self.ended_at_ms is not None
            and self.ended_at_ms < self.started_at_ms
        ):
            raise ValueError("ended_at_ms must not precede started_at_ms")

    @classmethod
    def with_embedding(
        cls,
        *,
        meeting_chunk_id: str,
        parent_meeting_id: str,
        user_id: str,
        sequence_no: int,
        content: str,
        embedding: Sequence[float],
        **kwargs: object,
    ) -> "MeetingChunkRecord":
        """Provider 응답 Sequence를 저장 가능한 불변 Tuple로 변환한다."""

        return cls(
            meeting_chunk_id=meeting_chunk_id,
            parent_meeting_id=parent_meeting_id,
            user_id=user_id,
            sequence_no=sequence_no,
            content=content,
            embedding=tuple(float(value) for value in embedding),
            **kwargs,
        )
