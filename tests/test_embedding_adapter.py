"""M4.1-03 Embedding Adapter·재임베딩 계약 테스트."""

from __future__ import annotations

import unittest
from unittest.mock import patch
import json
from uuid import uuid4

from app.domain.meeting_chunk import MeetingChunkRecord
from app.embeddings import EMBEDDING_DIMENSION, EmbeddingProviderError, OpenAICompatibleEmbeddingClient, ReembeddingService
from app.repositories.meeting_chunks import SQLiteMeetingChunkRepository


def vector(value: float) -> tuple[float, ...]:
    """계약 테스트용 고정 차원 Vector."""
    return (value,) * EMBEDDING_DIMENSION


class FakeProvider:
    """외부 호출 없이 성공·실패 경계를 검증하는 Provider Fixture."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def embed(self, texts: list[str]) -> list[tuple[float, ...]]:
        if self.fail:
            raise EmbeddingProviderError("fixture provider failure")
        return [vector(0.9) for _ in texts]


class EmbeddingAdapterTests(unittest.TestCase):
    """재임베딩 성공·실패·사용자 범위를 검증한다."""

    def setUp(self) -> None:
        self.repository = SQLiteMeetingChunkRepository()
        self.user_id = "user-a"
        self.chunk = MeetingChunkRecord(
            meeting_chunk_id=str(uuid4()), parent_meeting_id="meeting-1", user_id=self.user_id,
            sequence_no=0, content="원본 회의 문장", embedding=vector(0.1), embedding_version="1",
        )
        self.repository.create(self.chunk)

    def test_reembedding_updates_only_after_validated_provider_batch(self) -> None:
        result = ReembeddingService(FakeProvider(), self.repository).reembed([self.chunk], self.user_id)
        self.assertEqual(result.updated_count, 1)
        updated = self.repository.get(self.chunk.meeting_chunk_id, self.user_id)
        self.assertEqual(updated.embedding, vector(0.9))
        self.assertEqual(updated.embedding_version, "2")

    def test_provider_failure_preserves_existing_embedding_and_returns_warning(self) -> None:
        result = ReembeddingService(FakeProvider(fail=True), self.repository).reembed([self.chunk], self.user_id)
        self.assertEqual(result.updated_count, 0)
        self.assertTrue(result.warnings)
        self.assertEqual(self.repository.get(self.chunk.meeting_chunk_id, self.user_id).embedding, vector(0.1))

    def test_foreign_user_chunk_is_rejected_before_provider_call(self) -> None:
        with self.assertRaisesRegex(ValueError, "requested user"):
            ReembeddingService(FakeProvider(), self.repository).reembed([self.chunk], "other-user")

    def test_openai_model_alias_is_translated_for_direct_api(self) -> None:
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps({"data": [{"index": 0, "embedding": [0.1] * EMBEDDING_DIMENSION}]}).encode()

        client = OpenAICompatibleEmbeddingClient(
            base_url="https://api.openai.com/v1", api_key="test", model="openai/text-embedding-3-small"
        )
        with patch("app.embeddings.urlopen", return_value=Response()) as mocked:
            client.embed(["query"])
        body = json.loads(mocked.call_args.args[0].data)
        self.assertEqual(body["model"], "text-embedding-3-small")


if __name__ == "__main__":
    unittest.main()
