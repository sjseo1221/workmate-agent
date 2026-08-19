"""Transcript 조회와 STT Provider 경계 API."""

from fastapi import APIRouter, Depends, HTTPException
from app.internal_chat import _authenticated_user_or_assignee
from app.meeting_api import meeting_repository

router = APIRouter(prefix="/api/v1/meetings", tags=["transcript"])


@router.get("/{meeting_id}/transcript")
def list_transcript(meeting_id: str, user_id: str = Depends(_authenticated_user_or_assignee)) -> list[dict[str, object]]:
    """요청 사용자 회의의 임시·최종 Transcript를 반환한다."""
    if meeting_repository().get(meeting_id, user_id) is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    return meeting_repository().list_transcripts(meeting_id, user_id)
