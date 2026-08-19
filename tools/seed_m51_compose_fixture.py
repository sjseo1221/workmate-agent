"""Compose 검수용 비식별 회의와 최종 Transcript를 초기화한다."""

from datetime import datetime, timezone
from pathlib import Path

from app.domain.meeting import MeetingRecord
from app.repositories.meetings import SQLiteMeetingRepository


USER_ID = "00000000-0000-0000-0000-000000000001"
MEETING_ID = "00000000-0000-0000-0000-000000000101"


def main() -> None:
    path = Path(".runtime/meetings.sqlite3")
    repository = SQLiteMeetingRepository(path)
    repository.create(
        MeetingRecord(
            meeting_id=MEETING_ID,
            user_id=USER_ID,
            title="M5.1 비식별 검수 회의",
            started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    repository.save_transcript(
        MEETING_ID,
        USER_ID,
        0,
        "검수 회의에서 검색 기능을 다음 단계에 진행하기로 결정했다.",
        True,
        0,
        5000,
    )


if __name__ == "__main__":
    main()
