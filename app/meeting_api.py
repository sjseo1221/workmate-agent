"""회의 생성과 파일 입력 검증 API."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from functools import lru_cache
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from app.domain.meeting import MeetingRecord, RecordingRecord
from app.internal_chat import _authenticated_user_or_assignee
from app.repositories.meetings import SQLiteMeetingRepository
from app.storage.r2 import R2Config, R2StorageAdapter

MEETING_DB_PATH_ENV = "WORKMATE_MEETING_DB_PATH"
MAX_UPLOAD_BYTES = 500 * 1024 * 1024
ALLOWED_TYPES = {"audio/mpeg", "audio/wav", "audio/x-wav", "audio/mp4", "audio/webm", "audio/ogg"}
_AUDIO_EXTENSIONS = {"audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav", "audio/mp4": "m4a", "audio/webm": "webm", "audio/ogg": "ogg"}


@lru_cache(maxsize=1)
def r2_storage() -> R2StorageAdapter:
    """단일 R2 버킷 Adapter를 프로세스 전역에서 재사용한다."""
    return R2StorageAdapter(R2Config.from_env())


class MeetingCreateRequest(BaseModel):
    """회의 메타데이터 입력 계약."""
    model_config = ConfigDict(extra="forbid")
    meeting_id: str | None = Field(default=None, min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)
    started_at: datetime | None = None
    ended_at: datetime | None = None


class MeetingResponse(BaseModel):
    """회의 메타데이터 응답."""
    meeting_id: str
    user_id: str
    title: str
    started_at: datetime | None
    ended_at: datetime | None
    created_at: datetime | None
    has_analysis: bool = False
    """`analyze_meeting`이 저장한 요약이 있는지 — 자연어 Router가 "이미 분석된
    회의"를 알고 재분석 대신 `get_meeting_analysis`로 안내할 수 있게 한다
    (2026-08-17, 실사용 중 발견 — Context에 분석 여부가 없어 LLM이 "아직
    분석 안 한 회의"라고 근거 없이 단정했다)."""


class RecordingResponse(BaseModel):
    """녹음 원본을 노출하지 않는 메타데이터 응답."""
    recording_id: str
    meeting_id: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    object_key: str | None


@lru_cache(maxsize=1)
def meeting_repository() -> SQLiteMeetingRepository:
    """환경 변수에 지정된 회의 SQLite 저장소를 반환한다."""
    return SQLiteMeetingRepository(os.getenv(MEETING_DB_PATH_ENV, ".runtime/meetings.sqlite3"))


router = APIRouter(prefix="/api/v1/meetings", tags=["meetings"])


def _meeting_response(record: MeetingRecord) -> MeetingResponse:
    """`summary` 저장 여부로 `has_analysis`를 계산해 응답에 싣는다."""

    response = MeetingResponse.model_validate(record, from_attributes=True)
    return response.model_copy(update={"has_analysis": record.summary is not None})


@router.post("", response_model=MeetingResponse, status_code=status.HTTP_201_CREATED)
def create_meeting(payload: MeetingCreateRequest, user_id: str = Depends(_authenticated_user_or_assignee)) -> MeetingResponse:
    """회의 메타데이터를 멱등 생성한다."""
    record = meeting_repository().create(MeetingRecord(payload.meeting_id or str(uuid4()), user_id, payload.title, payload.started_at, payload.ended_at))
    return _meeting_response(record)


@router.get("", response_model=list[MeetingResponse])
def list_meetings(user_id: str = Depends(_authenticated_user_or_assignee)) -> list[MeetingResponse]:
    """요청 사용자 회의만 반환한다."""
    return [_meeting_response(item) for item in meeting_repository().list(user_id)]


@router.get("/{meeting_id}", response_model=MeetingResponse)
def get_meeting(meeting_id: str, user_id: str = Depends(_authenticated_user_or_assignee)) -> MeetingResponse:
    """요청 사용자의 회의 상세만 반환한다.

    `include_deleted=True`로 조회한다 — 목록(`GET /api/v1/meetings`)에는 삭제된
    회의가 안 보이지만, Task의 "회의록에서 보기"(`meeting_evidence`)처럼 정확한
    ID를 이미 아는 직접 링크로 찾아온 경우는 계속 열람할 수 있어야 한다
    (2026-08-17, 사용자 요청 — 회의를 삭제해도 그 회의에서 만든 Task의 근거
    링크가 계속 연결돼야 한다). 소유자 검사는 그대로라 다른 사용자의 회의는
    여전히 볼 수 없다.
    """

    record = meeting_repository().get(meeting_id, user_id, include_deleted=True)
    if record is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    return _meeting_response(record)


@router.delete("/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def delete_meeting(meeting_id: str, user_id: str = Depends(_authenticated_user_or_assignee)) -> None:
    """회의를 Soft Delete한다(2026-08-17, 사용자 요청) — 목록·상세 조회에서 더는 보이지 않는다.

    Recording·Transcript·Action Item·검색 색인(Postgres `meeting_chunks`)은 지우지 않는다 —
    Task의 `deleted_at` Soft Delete와 같은 범위 선택이다(완전 삭제는 R2 원본 음성까지
    함께 지워야 해 범위가 더 크다).
    """

    if not meeting_repository().delete(meeting_id, user_id):
        raise HTTPException(status_code=404, detail="meeting not found")


@router.post("/{meeting_id}/recordings", response_model=RecordingResponse, status_code=status.HTTP_201_CREATED)
async def upload_recording(meeting_id: str, file: UploadFile = File(...), user_id: str = Depends(_authenticated_user_or_assignee)) -> RecordingResponse:
    """MIME·크기·checksum을 검증하고 원본을 R2에 저장한다.

    07-technical-specification.md §13.1: 업로드 완료 후 `HeadObject`로
    MIME·크기·checksum을 확인한 뒤에만 원본을 확정한다(2026-08-15 R2 연동,
    `workmate-ui-integration-gaps.md` #20 — 이전에는 메타데이터만 저장하고
    원본 바이트 자체를 유실했다).
    """
    if meeting_repository().get(meeting_id, user_id) is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=415, detail="unsupported audio MIME type")
    digest = hashlib.sha256()
    size = 0
    body = bytearray()
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="recording exceeds 500MB")
        digest.update(chunk)
        body.extend(chunk)
    checksum = digest.hexdigest()
    ext = _AUDIO_EXTENSIONS.get(file.content_type, "bin")
    object_key = f"users/{user_id}/meetings/{meeting_id}/source/{checksum}.{ext}"
    try:
        storage = r2_storage()
        storage.put_object(object_key, bytes(body), file.content_type)
        head = storage.head(object_key)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"R2 storage is not configured: {exc}") from exc
    if int(head.get("ContentLength", -1)) != size:
        raise HTTPException(status_code=502, detail="R2 upload verification failed: size mismatch")
    record = meeting_repository().add_recording(RecordingRecord(str(uuid4()), meeting_id, user_id, file.filename or "recording", file.content_type, size, checksum, object_key))
    meeting_repository().set_source_audio_uri(meeting_id, user_id, object_key)
    return RecordingResponse.model_validate(record, from_attributes=True)

