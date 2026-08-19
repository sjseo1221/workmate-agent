"""M3/M4 회의 분석·검색을 A2A 전송 경계에 연결하는 Workflow."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, time, timezone
import json
import os
import re
from typing import Protocol
from uuid import uuid4

from app.domain.meeting_chunk import MeetingChunkRecord
from app.embeddings import EmbeddingProvider, OpenAICompatibleEmbeddingClient
from app.meeting_analysis import MeetingAnalysis, analyze_transcript
from app.meeting_answer import MeetingAnswerService
from app.repositories.meeting_chunks import HybridSearchHit, MeetingChunkRepository, PostgresMeetingChunkRepository
from app.repositories.meetings import SQLiteMeetingRepository
from app.stt import OpenAICompatibleSttClient, SttProviderError
from app.workflows.registry import WorkflowRequest, WorkflowResult

# 검색 Chunk 하나가 담을 최대 글자 수. 파일 기반 STT는 회의 전체를 한 Row로
# 합쳐 반환하므로, 문장 경계로 재분할하지 않으면 Chunk 하나가 회의 전체가
# 돼 Dense/Trigram 검색의 근거 구간이 지나치게 넓어진다.
_CHUNK_MAX_CHARS = 400
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?다요])\s+")

# `Meeting.tsx`의 `defaultMeetingTitle()`이 만드는 자리표시자 제목과 정확히
# 같은 모양이다("회의 8월 17일 16:06") — 녹음 시점엔 아직 내용이 없어 날짜·
# 시각으로만 채운 제목이라, 분석이 끝나 내용을 요약할 수 있게 되면 이
# 패턴일 때만 실제 제목으로 바꾼다(사용자가 직접 지은 제목은 건드리지
# 않는다). 2026-08-17, 사용자 요청 — 회의 목록에 자동 생성 제목을 보여달라.
_DEFAULT_MEETING_TITLE_PATTERN = re.compile(r"^회의 \d{1,2}월 \d{1,2}일 \d{2}:\d{2}$")

# R2 Object Key 확장자로 STT 업로드 시 보낼 Content-Type을 역으로 추정한다.
# `app/meeting_api.py`의 `_AUDIO_EXTENSIONS`(MIME → 확장자)와 반대 방향 매핑이다.
_CONTENT_TYPE_BY_EXTENSION = {"mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4", "webm": "audio/webm", "ogg": "audio/ogg"}


class MeetingTranscriptRepository(Protocol):
    """회의 분석에 필요한 사용자 범위 Transcript 저장 계약."""

    def get(self, meeting_id: str, user_id: str): ...

    def list_transcripts(self, meeting_id: str, user_id: str) -> list[dict[str, object]]: ...

    def save_transcript(
        self,
        meeting_id: str,
        user_id: str,
        chunk_no: int,
        text: str,
        is_final: bool,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> dict[str, object]: ...

    def upsert_action(
        self,
        action_item_id: str,
        meeting_id: str,
        user_id: str,
        title: str,
        evidence_text: str,
    ) -> dict[str, object]: ...


class ObjectStorage(Protocol):
    """분석 시작 시 원본 음성을 내려받는 데 필요한 최소 저장소 계약."""

    def get_object(self, key: str) -> bytes: ...


class SttClient(Protocol):
    """분석 시작 시 원본 음성을 최종 Transcript로 바꾸는 데 필요한 계약."""

    def transcribe(self, audio_bytes: bytes, *, filename: str = ..., content_type: str = ..., language: str = ...) -> str: ...


def _default_storage() -> ObjectStorage:
    """운영 R2 Adapter를 지연 import한다 — `app.meeting_api`와의 순환 import를 피한다."""

    from app.meeting_api import r2_storage

    return r2_storage()


class MeetingSearchRepository(Protocol):
    """회의 Hybrid 검색에 필요한 Repository 계약."""

    def search_hybrid(
        self,
        query: str,
        query_embedding: tuple[float, ...],
        user_id: str,
        *,
        meeting_id: str | None = None,
        started_from: datetime | None = None,
        ended_to: datetime | None = None,
        limit: int = 20,
    ) -> list[HybridSearchHit]: ...


Analyzer = Callable[[str], MeetingAnalysis]


def _meeting_repository() -> SQLiteMeetingRepository:
    """회의 API와 동일한 영속 SQLite 저장소를 반환한다."""

    return SQLiteMeetingRepository(os.getenv("WORKMATE_MEETING_DB_PATH", ".runtime/meetings.sqlite3"))


def _search_dependencies() -> tuple[MeetingSearchRepository, EmbeddingProvider]:
    """운영 PostgreSQL 검색 저장소와 Embedding Provider를 구성한다."""

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is required for search_meetings")
    return PostgresMeetingChunkRepository(dsn), OpenAICompatibleEmbeddingClient()


def _split_into_search_chunks(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """최종 Transcript Row를 검색용 Chunk 후보로 나눈다.

    Row가 여럿이면(발화 단위로 저장된 최종 Transcript) 이미 `start_ms`/
    `end_ms` 근거가 있는 Row 자체를 Chunk 단위로 쓴다. Row가 하나뿐이고
    길이가 `_CHUNK_MAX_CHARS` 이내면 그 Row의 timestamp를 그대로 쓴다.
    지금처럼 파일 기반 STT가 회의 전체를 Row 하나로 합쳐 반환했는데 그
    길이가 기준을 넘으면 문장 경계로 나눠 묶는다 — 이때는 문장 단위
    timestamp를 복원할 방법이 없어 `start_ms`/`end_ms`를 `None`으로
    남긴다.
    """

    non_empty = [row for row in rows if str(row["text"]).strip()]
    if not non_empty:
        return []
    if len(non_empty) > 1:
        return [{"content": str(row["text"]).strip(), "start_ms": row.get("start_ms"), "end_ms": row.get("end_ms")} for row in non_empty]
    only_row = non_empty[0]
    only_text = str(only_row["text"]).strip()
    if len(only_text) <= _CHUNK_MAX_CHARS:
        return [{"content": only_text, "start_ms": only_row.get("start_ms"), "end_ms": only_row.get("end_ms")}]
    sentences = [sentence.strip() for sentence in _SENTENCE_BOUNDARY.split(only_text) if sentence.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip() if current else sentence
        if current and len(candidate) > _CHUNK_MAX_CHARS:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return [{"content": chunk, "start_ms": None, "end_ms": None} for chunk in chunks]


async def _index_meeting_chunks(
    meeting_id: str,
    user_id: str,
    rows: list[dict[str, object]],
    *,
    meeting_title: str,
    meeting_started_at: datetime | None,
    meeting_ended_at: datetime | None,
    chunk_repository: MeetingChunkRepository | None,
    embedding_provider: EmbeddingProvider | None,
) -> dict[str, object] | None:
    """최종 Transcript를 검색용 Chunk로 나눠 Embedding과 함께 저장한다.

    `search_meetings`은 별도 Skill이라 이 색인이 실패해도 이미 계산된
    분석 결과(요약·Action Item)는 그대로 반환해야 한다 — PostgreSQL과
    Embedding Provider가 아직 준비되지 않은 환경(예: 로컬 SQLite 전용
    개발 환경)에서도 핵심 분석 흐름을 막지 않기 위해서다(2026-08-15,
    14번 갭 문서 #23). 실패하면 재시도 가능한 경고를 반환한다.
    """

    pieces = _split_into_search_chunks(rows)
    if not pieces:
        return None
    try:
        repo = chunk_repository
        provider = embedding_provider
        if repo is None or provider is None:
            default_repo, default_provider = _search_dependencies()
            repo = repo or default_repo
            provider = provider or default_provider
        vectors = await asyncio.to_thread(provider.embed, [piece["content"] for piece in pieces])
        for sequence_no, (piece, embedding) in enumerate(zip(pieces, vectors)):
            record = MeetingChunkRecord.with_embedding(
                meeting_chunk_id=str(uuid4()),
                parent_meeting_id=meeting_id,
                user_id=user_id,
                sequence_no=sequence_no,
                content=piece["content"],
                embedding=embedding,
                started_at_ms=piece["start_ms"],
                ended_at_ms=piece["end_ms"],
                meeting_title=meeting_title,
                meeting_started_at=meeting_started_at,
                meeting_ended_at=meeting_ended_at,
            )
            await asyncio.to_thread(repo.create, record)
        return None
    except Exception as exc:  # noqa: BLE001 - 색인 실패를 경고로 낮춰 분석 결과 반환을 막지 않는다.
        return {
            "source": "meeting",
            "code": "MEETING_CHUNK_INDEXING_UNAVAILABLE",
            "message": str(exc),
            "retryable": True,
            "last_success_at": None,
        }


def build_analyze_meeting_workflow(
    *,
    repository: MeetingTranscriptRepository | None = None,
    analyzer: Analyzer = analyze_transcript,
    stt_client_factory: Callable[[], SttClient] = OpenAICompatibleSttClient,
    storage_factory: Callable[[], ObjectStorage] = _default_storage,
    chunk_repository: MeetingChunkRepository | None = None,
    embedding_provider: EmbeddingProvider | None = None,
):
    """저장된 최종 Transcript를 실제 LLM 분석 Workflow에 연결한다.

    최종 Transcript가 아직 없어도 회의에 확정된 원본 음성
    (`Meeting.source_audio_uri`)이 있으면 R2에서 내려받아 STT부터 자동
    수행한다 — 사용자가 별도 트리거 없이 기존 "분석 실행" Skill 호출
    하나로 녹음→최종 Transcript→분석까지 이어지게 하기 위해서다
    (2026-08-15, 14번 갭 문서 #21).

    분석이 끝나면 최종 Transcript를 검색용 Chunk로 나눠 Embedding과 함께
    `meeting_chunks`에 저장한다(#23) — `search_meetings` Skill이 근거를
    찾을 수 있으려면 이 색인이 선행돼야 한다. 색인은 PostgreSQL·Embedding
    Provider가 필요해 SQLite 기반인 나머지 회의 저장소보다 준비 조건이
    엄격하므로, 실패해도 이미 계산된 요약·Action Item은 그대로 반환하고
    재시도 가능한 경고만 덧붙인다.
    """

    async def execute(request: WorkflowRequest) -> WorkflowResult:
        payload = request.payload
        meeting_id = str(payload.get("meeting_id", "")).strip()
        if not meeting_id:
            raise ValueError("meeting_id is required")
        repo = repository or _meeting_repository()
        meeting = repo.get(meeting_id, request.user_id)
        if meeting is None:
            raise ValueError("meeting not found for requested user")
        rows = [row for row in repo.list_transcripts(meeting_id, request.user_id) if bool(row.get("is_final"))]
        if not rows:
            rows = await _transcribe_source_audio(
                repo, meeting, meeting_id, request.user_id, stt_client_factory, storage_factory
            )
        transcript = "\n".join(str(row["text"]) for row in rows)
        analysis = await asyncio.to_thread(analyzer, transcript)
        action_items: list[dict[str, object]] = []
        source_refs = [str(row["transcript_id"]) for row in rows]
        for item in analysis.action_items:
            evidence_row = next((row for row in rows if item.evidence_text in str(row["text"])), None)
            if evidence_row is None:
                raise ValueError("action item evidence is not in a final transcript")
            # `description`·`due_at`·`assignee_user_id`·`evidence_span`도 함께
            # 저장해 `GET .../analysis` 재조회(#10)와 Task 상세의
            # `meeting_evidence`(#11)가 다시 계산 없이 그대로 쓸 수 있게 한다
            # (2026-08-16, 17번 갭 문서).
            repo.upsert_action(
                item.action_item_id, meeting_id, request.user_id, item.title, item.evidence_text,
                due_at=item.due_at, assignee_user_id=item.assignee_id,
                start_ms=item.start_ms, end_ms=item.end_ms,
                meeting_chunk_id=str(evidence_row["transcript_id"]),
            )
            action_items.append(
                {
                    "action_item_id": item.action_item_id,
                    "title": item.title,
                    "description": None,
                    "assignee_user_id": item.assignee_id,
                    "due_at": item.due_at,
                    "approval_status": "pending",
                    "confidence": 1.0,
                    "evidence_span": {
                        "meeting_chunk_ids": [str(evidence_row["transcript_id"])],
                        "start_ms": item.start_ms,
                        "end_ms": item.end_ms,
                    },
                    "evidence_text": item.evidence_text,
                }
            )
        repo.set_summary(meeting_id, request.user_id, analysis.summary)
        if _DEFAULT_MEETING_TITLE_PATTERN.match(meeting.title):
            repo.set_title(meeting_id, request.user_id, analysis.title)
        warnings = []
        indexing_warning = await _index_meeting_chunks(
            meeting_id,
            request.user_id,
            rows,
            meeting_title=meeting.title,
            # `started_at`은 입력 계약상 선택값이고(`MeetingCreateRequest`) 실제로 프론트
            # 녹음 화면(`Meeting.tsx`)이 값을 보내지 않아 사실상 항상 `None`이다 —
            # `created_at`(항상 채워짐)으로 대신해 검색 결과 인용에 쓸 실제 날짜가
            # 없는 경우가 없도록 한다(2026-08-16, 14번 갭 문서).
            meeting_started_at=meeting.started_at or meeting.created_at,
            meeting_ended_at=meeting.ended_at,
            chunk_repository=chunk_repository,
            embedding_provider=embedding_provider,
        )
        if indexing_warning is not None:
            warnings.append(indexing_warning)
        data = {
            "meeting_id": meeting_id,
            "summary": analysis.summary,
            "action_items": action_items,
            "transcript_ref": source_refs[0],
            "source_refs": list(dict.fromkeys([*source_refs, *(item["action_item_id"] for item in action_items)])),
        }
        typed_result = {"type": "meeting_analysis", "data": data}
        return WorkflowResult(
            artifact_name="meeting_analysis",
            artifact_description="LLM meeting analysis grounded in the user's final transcript.",
            text=json.dumps(typed_result, ensure_ascii=False, sort_keys=True),
            data=typed_result,
            warnings=warnings,
            mock=False,
        )

    return execute


async def _transcribe_source_audio(
    repo: MeetingTranscriptRepository,
    meeting: object,
    meeting_id: str,
    user_id: str,
    stt_client_factory: Callable[[], SttClient],
    storage_factory: Callable[[], ObjectStorage],
) -> list[dict[str, object]]:
    """확정된 원본 음성을 R2에서 내려받아 STT로 최종 Transcript를 만든다.

    회의에 원본 음성이 아직 없으면(녹음/업로드를 아직 안 함) 기존과 같이
    "최종 Transcript가 필요하다"는 사용자 오류로 남긴다 — STT를 시도할
    입력 자체가 없기 때문이다. R2/STT Provider 자체가 설정되지 않았거나
    호출이 실패하면 원인을 그대로 드러내 재시도 여부를 판단할 수 있게
    한다(daily_briefing.py처럼 경고로 삼키지 않는다 — 회의 분석은 Transcript
    없이는 의미 있는 부분 결과가 없기 때문).
    """

    object_key = getattr(meeting, "source_audio_uri", None)
    if not object_key:
        raise ValueError("final transcript is required")
    try:
        storage = storage_factory()
        audio_bytes = storage.get_object(object_key)
    except RuntimeError as exc:
        raise ValueError(f"source audio could not be retrieved from storage: {exc}") from exc
    extension = object_key.rsplit(".", 1)[-1].lower() if "." in object_key else ""
    content_type = _CONTENT_TYPE_BY_EXTENSION.get(extension, "audio/wav")
    filename = object_key.rsplit("/", 1)[-1] if "/" in object_key else object_key
    try:
        client = stt_client_factory()
        text = client.transcribe(audio_bytes, filename=filename, content_type=content_type, language="ko")
    except SttProviderError as exc:
        raise ValueError(f"speech-to-text transcription failed: {exc}") from exc
    repo.save_transcript(meeting_id, user_id, 0, text, True)
    return [row for row in repo.list_transcripts(meeting_id, user_id) if bool(row.get("is_final"))]


def _date_boundary(value: object, *, end: bool) -> datetime | None:
    """검색 Filter의 날짜를 UTC 경계 시각으로 변환한다."""

    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value)).date()
    return datetime.combine(parsed, time.max if end else time.min, tzinfo=timezone.utc)


def build_search_meetings_workflow(
    *,
    repository: MeetingSearchRepository | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    answer_service: MeetingAnswerService | None = None,
):
    """Embedding·Hybrid 검색과 근거 기반 답변을 A2A 결과로 변환한다."""

    async def execute(request: WorkflowRequest) -> WorkflowResult:
        payload = request.payload
        query = str(payload.get("query", "")).strip()
        if not query:
            raise ValueError("query is required")
        limit = int(payload.get("limit", 5))
        filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
        if filters.get("participants"):
            raise ValueError("participant filtering is not supported by the current meeting schema")
        meeting_ids = [str(value) for value in filters.get("meeting_ids", [])]
        repo, provider = (repository, embedding_provider)
        if repo is None or provider is None:
            default_repo, default_provider = _search_dependencies()
            repo = repo or default_repo
            provider = provider or default_provider
        vectors = await asyncio.to_thread(provider.embed, [query])
        query_embedding = vectors[0]
        date_from = _date_boundary(filters.get("date_from"), end=False)
        date_to = _date_boundary(filters.get("date_to"), end=True)
        targets: list[str | None] = meeting_ids or [None]
        combined: dict[str, HybridSearchHit] = {}
        for meeting_id in targets:
            hits = await asyncio.to_thread(
                repo.search_hybrid,
                query,
                query_embedding,
                request.user_id,
                meeting_id=meeting_id,
                started_from=date_from,
                ended_to=date_to,
                limit=limit,
            )
            for hit in hits:
                previous = combined.get(hit.meeting_chunk_id)
                if previous is None or hit.rrf_score > previous.rrf_score:
                    combined[hit.meeting_chunk_id] = hit
        ranked = sorted(combined.values(), key=lambda hit: (-hit.rrf_score, hit.sequence_no, hit.meeting_chunk_id))[:limit]
        answer = (answer_service or MeetingAnswerService()).answer_from_hits(query, ranked, max_sources=limit)
        sources = []
        for source in answer.sources:
            if source.meeting_started_at is None:
                raise ValueError("meeting date is required for a cited source")
            sources.append(
                {
                    "meeting_id": source.meeting_id,
                    "meeting_title": source.meeting_title,
                    "meeting_date": source.meeting_started_at.date().isoformat(),
                    "speaker": None,
                    "meeting_chunk_id": source.meeting_chunk_id,
                    "quote": source.evidence_text,
                }
            )
        typed_result = {
            "type": "grounded_answer",
            "data": {
                "answer": answer.answer,
                "sources": sources,
                "insufficient_evidence": not answer.grounded,
            },
        }
        warnings = [
            {
                "source": "meeting",
                "code": "INSUFFICIENT_EVIDENCE",
                "message": warning,
                "retryable": False,
                "last_success_at": None,
            }
            for warning in answer.warnings
        ]
        return WorkflowResult(
            artifact_name="grounded_answer",
            artifact_description="Grounded meeting answer with exact chunk citations.",
            text=json.dumps(typed_result, ensure_ascii=False, sort_keys=True),
            data=typed_result,
            warnings=warnings,
            mock=False,
        )

    return execute


__all__ = ["build_analyze_meeting_workflow", "build_search_meetings_workflow"]
