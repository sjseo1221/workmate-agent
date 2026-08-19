"""OpenAI 호환 Embedding Provider와 재임베딩 Workflow 경계."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from collections.abc import Sequence
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.domain.meeting_chunk import MeetingChunkRecord

EMBEDDING_MODEL = "openai/text-embedding-3-small"
EMBEDDING_DIMENSION = 1536
EMBEDDING_PROVIDER_MODEL = "text-embedding-3-small"


class EmbeddingProviderError(RuntimeError):
    """Embedding Provider 호출·응답 검증 실패."""


class EmbeddingProvider(Protocol):
    """문장 목록을 고정 차원 Vector로 변환하는 Provider 계약."""

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]: ...


class OpenAICompatibleEmbeddingClient:
    """OpenAI 호환 `/embeddings` API Client.

    API Key는 환경변수에서만 읽고 로그·예외 메시지에 포함하지 않는다.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = (base_url or os.getenv("EMBEDDING_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("EMBEDDING_API_KEY", "")
        configured_model = model or os.getenv("EMBEDDING_MODEL", EMBEDDING_MODEL)
        self.model = configured_model
        self.provider_model = EMBEDDING_PROVIDER_MODEL if configured_model in {EMBEDDING_MODEL, EMBEDDING_PROVIDER_MODEL} else configured_model
        self.timeout_seconds = timeout_seconds
        if not self.base_url or not self.api_key:
            raise EmbeddingProviderError("EMBEDDING_BASE_URL and EMBEDDING_API_KEY are required")
        if self.model not in {EMBEDDING_MODEL, EMBEDDING_PROVIDER_MODEL}:
            raise EmbeddingProviderError(f"unsupported embedding model: {self.model}")

    def _endpoint(self) -> str:
        return self.base_url if self.base_url.endswith("/embeddings") else f"{self.base_url}/embeddings"

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """입력 순서를 보존한 1536차원 Embedding을 반환한다."""
        if not texts or any(not text.strip() for text in texts):
            raise EmbeddingProviderError("embedding input must contain non-empty text")
        body = json.dumps({"model": self.provider_model, "input": list(texts)}).encode("utf-8")
        request = Request(
            self._endpoint(),
            data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise EmbeddingProviderError("embedding provider request failed") from exc
        try:
            rows = sorted(payload["data"], key=lambda row: row["index"])
            vectors = [tuple(float(value) for value in row["embedding"]) for row in rows]
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingProviderError("embedding provider response is invalid") from exc
        if len(vectors) != len(texts) or any(len(vector) != EMBEDDING_DIMENSION for vector in vectors):
            raise EmbeddingProviderError("embedding provider returned an unexpected dimension")
        return vectors


@dataclass(frozen=True, slots=True)
class ReembeddingResult:
    """재임베딩 결과와 업무 응답에 전달할 경고."""

    updated_count: int
    warnings: tuple[str, ...] = ()


class ReembeddingService:
    """기존 Chunk를 보존하면서 검증된 Vector만 원자적으로 갱신한다."""

    def __init__(self, provider: EmbeddingProvider, repository: object) -> None:
        self.provider = provider
        self.repository = repository

    def reembed(self, chunks: Sequence[MeetingChunkRecord], user_id: str) -> ReembeddingResult:
        """사용자 소유 Chunk만 재임베딩하고 Provider 실패 시 기존 값을 보존한다."""
        if any(chunk.user_id != user_id for chunk in chunks):
            raise ValueError("all chunks must belong to the requested user")
        if not chunks:
            return ReembeddingResult(updated_count=0)
        try:
            vectors = self.provider.embed([chunk.content for chunk in chunks])
            if len(vectors) != len(chunks) or any(len(vector) != EMBEDDING_DIMENSION for vector in vectors):
                raise EmbeddingProviderError("embedding batch dimension mismatch")
            replacements = [
                MeetingChunkRecord(
                    meeting_chunk_id=chunk.meeting_chunk_id,
                    parent_meeting_id=chunk.parent_meeting_id,
                    user_id=chunk.user_id,
                    sequence_no=chunk.sequence_no,
                    content=chunk.content,
                    embedding=vector,
                    speaker=chunk.speaker,
                    started_at_ms=chunk.started_at_ms,
                    ended_at_ms=chunk.ended_at_ms,
                    embedding_model=EMBEDDING_MODEL,
                    embedding_version=str(int(chunk.embedding_version) + 1),
                    created_at=chunk.created_at,
                )
                for chunk, vector in zip(chunks, vectors, strict=True)
            ]
            for replacement in replacements:
                if self.repository.update(replacement, user_id) is None:
                    raise EmbeddingProviderError("chunk disappeared during reembedding")
        except EmbeddingProviderError as exc:
            return ReembeddingResult(updated_count=0, warnings=(str(exc),))
        return ReembeddingResult(updated_count=len(chunks))
