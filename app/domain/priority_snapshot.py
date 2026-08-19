"""우선순위 계산 결과를 저장하기 위한 Domain 값 객체."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.domain.priority import ScoreBreakdown


@dataclass(frozen=True, slots=True)
class PrioritySnapshotRecord:
    """사용자·Task별 우선순위 계산 결과."""

    priority_snapshot_id: str
    recipient_user_id: str
    ranked_task_id: str
    calculated_at: datetime
    as_of: datetime
    rank: int
    score: int
    score_breakdown: ScoreBreakdown
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        """순위·점수와 기준 시각의 기본 불변식을 검증한다."""

        if not self.recipient_user_id or not self.ranked_task_id:
            raise ValueError("snapshot user and task are required")
        if self.calculated_at.tzinfo is None or self.as_of.tzinfo is None:
            raise ValueError("snapshot timestamps must be timezone-aware")
        if self.rank < 1:
            raise ValueError("rank must be positive")
        if self.score != self.score_breakdown.total:
            raise ValueError("score must equal score_breakdown total")
