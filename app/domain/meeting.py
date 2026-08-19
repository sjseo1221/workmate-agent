"""회의 입력 Domain 모델."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class MeetingRecord:
    """사용자 범위가 적용된 회의 메타데이터와 업로드 상태."""

    meeting_id: str
    user_id: str
    title: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    created_at: datetime | None = None
    source_audio_uri: str | None = None
    """확정된 원본 음성의 R2 Object Key. 만료되는 Presigned URL은 저장하지 않는다(03 §1.2)."""
    summary: str | None = None
    """`analyze_meeting`이 만든 LLM 요약 — 재조회(`GET .../analysis`)를 위해 저장한다(2026-08-16, 17번 갭 문서 #10)."""


@dataclass(frozen=True, slots=True)
class RecordingRecord:
    """회의 녹음의 비식별 메타데이터. 원본 음성은 저장하지 않는다."""

    recording_id: str
    meeting_id: str
    user_id: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    object_key: str | None = None
    created_at: datetime | None = None
