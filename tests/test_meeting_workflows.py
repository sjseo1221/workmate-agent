"""M3/M4 실제 회의 Workflow의 A2A Registry 연결 검증."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from app.domain.meeting import MeetingRecord
from app.meeting_analysis import ActionItem, MeetingAnalysis
from app.repositories.meeting_chunks import HybridSearchHit, SQLiteMeetingChunkRepository
from app.repositories.meetings import SQLiteMeetingRepository
from app.stt import SttProviderError
from app.workflows.meetings import build_analyze_meeting_workflow, build_search_meetings_workflow
from app.workflows.registry import WorkflowRequest


def request(skill_id: str, payload: dict[str, object]) -> WorkflowRequest:
    """테스트용 전송 독립 Workflow 요청을 만든다."""

    return WorkflowRequest(skill_id, "task-1", "thread-1", "message-1", "user-1", payload)


class FakeEmbeddingProvider:
    """검색 Workflow 단위 테스트용 1536차원 Embedding Provider."""

    def embed(self, texts):
        return [(0.1,) * 1536 for _ in texts]


class FakeSearchRepository:
    """사용자 범위 Hybrid 검색 결과를 반환하는 테스트 Repository."""

    def search_hybrid(self, query, query_embedding, user_id, **kwargs):
        return [
            HybridSearchHit(
                meeting_chunk_id="chunk-1",
                meeting_id="meeting-1",
                user_id=user_id,
                content="예산은 1천만원으로 결정했다.",
                meeting_title="예산 회의",
                meeting_started_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
                sequence_no=0,
                rrf_score=0.03,
                dense_rank=1,
                keyword_rank=1,
            )
        ]


class MeetingWorkflowTests(unittest.IsolatedAsyncioTestCase):
    """회의 분석·검색이 Mock 없이 구조화 결과를 반환하는지 확인한다."""

    async def test_analyze_meeting_uses_final_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteMeetingRepository(Path(directory) / "meetings.sqlite3")
            repository.create(MeetingRecord("meeting-1", "user-1", "회의"))
            repository.save_transcript(
                "meeting-1", "user-1", 0, "결과를 내일까지 공유한다.", True, 0, 1000
            )

            def analyzer(transcript: str) -> MeetingAnalysis:
                self.assertIn("결과를 내일까지 공유한다.", transcript)
                return MeetingAnalysis(
                    title="결과 공유 회의",
                    summary="공유 일정을 확정했다.",
                    action_items=[
                        ActionItem(
                            action_item_id="action-1",
                            title="결과 공유",
                            evidence_text="결과를 내일까지 공유한다.",
                            start_ms=0,
                            end_ms=1000,
                        )
                    ],
                )

            result = await build_analyze_meeting_workflow(repository=repository, analyzer=analyzer)(
                request("analyze_meeting", {"meeting_id": "meeting-1"})
            )
            self.assertFalse(result.mock)
            self.assertEqual(result.data["type"], "meeting_analysis")
            self.assertEqual(result.data["data"]["action_items"][0]["approval_status"], "pending")

    async def test_analyze_meeting_replaces_a_placeholder_title_with_the_analyzed_title(self) -> None:
        """회의 삭제 기능 추가·제목 자동 생성 요청(2026-08-17)의 회귀 테스트 —
        녹음 시점의 날짜·시각 자리표시자 제목("회의 8월 17일 16:06", `Meeting.tsx`의
        `defaultMeetingTitle()`)을, 분석이 끝나면 실제 내용을 요약한 제목으로
        바꿔야 한다."""

        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteMeetingRepository(Path(directory) / "meetings.sqlite3")
            repository.create(MeetingRecord("meeting-1", "user-1", "회의 8월 17일 16:06"))
            repository.save_transcript("meeting-1", "user-1", 0, "결과를 내일까지 공유한다.", True, 0, 1000)

            def analyzer(transcript: str) -> MeetingAnalysis:
                return MeetingAnalysis(title="결과 공유 확정 회의", summary="공유 일정을 확정했다.", action_items=[])

            await build_analyze_meeting_workflow(repository=repository, analyzer=analyzer)(
                request("analyze_meeting", {"meeting_id": "meeting-1"})
            )
            record = repository.get("meeting-1", "user-1")
            assert record is not None
            self.assertEqual(record.title, "결과 공유 확정 회의")

    async def test_analyze_meeting_keeps_a_user_provided_title(self) -> None:
        """자리표시자 패턴이 아닌(사용자가 직접 지은) 제목은 분석 뒤에도 그대로 둔다."""

        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteMeetingRepository(Path(directory) / "meetings.sqlite3")
            repository.create(MeetingRecord("meeting-1", "user-1", "스프린트 회고"))
            repository.save_transcript("meeting-1", "user-1", 0, "결과를 내일까지 공유한다.", True, 0, 1000)

            def analyzer(transcript: str) -> MeetingAnalysis:
                return MeetingAnalysis(title="결과 공유 확정 회의", summary="공유 일정을 확정했다.", action_items=[])

            await build_analyze_meeting_workflow(repository=repository, analyzer=analyzer)(
                request("analyze_meeting", {"meeting_id": "meeting-1"})
            )
            record = repository.get("meeting-1", "user-1")
            assert record is not None
            self.assertEqual(record.title, "스프린트 회고")

    async def test_analyze_meeting_transcribes_source_audio_when_no_final_transcript(self) -> None:
        """최종 Transcript가 없어도 확정된 원본 음성이 있으면 STT부터 자동
        수행해 분석까지 이어져야 한다(2026-08-15, 14번 갭 문서 #21)."""

        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteMeetingRepository(Path(directory) / "meetings.sqlite3")
            repository.create(MeetingRecord("meeting-1", "user-1", "회의"))
            repository.set_source_audio_uri("meeting-1", "user-1", "users/user-1/meetings/meeting-1/source/abc.wav")

            class FakeStorage:
                def get_object(self, key: str) -> bytes:
                    self.requested_key = key
                    return b"fake-wav-bytes"

            class FakeSttClient:
                def transcribe(self, audio_bytes, *, filename, content_type, language):
                    self.received = (audio_bytes, filename, content_type, language)
                    return "결과를 내일까지 공유한다."

            fake_storage = FakeStorage()
            fake_stt = FakeSttClient()

            def analyzer(transcript: str) -> MeetingAnalysis:
                self.assertIn("결과를 내일까지 공유한다.", transcript)
                return MeetingAnalysis(title="공유 일정 회의", summary="공유 일정을 확정했다.", action_items=[])

            result = await build_analyze_meeting_workflow(
                repository=repository,
                analyzer=analyzer,
                stt_client_factory=lambda: fake_stt,
                storage_factory=lambda: fake_storage,
            )(request("analyze_meeting", {"meeting_id": "meeting-1"}))

            self.assertFalse(result.mock)
            self.assertEqual(fake_storage.requested_key, "users/user-1/meetings/meeting-1/source/abc.wav")
            self.assertEqual(fake_stt.received, (b"fake-wav-bytes", "abc.wav", "audio/wav", "ko"))
            saved = [row for row in repository.list_transcripts("meeting-1", "user-1") if row["is_final"]]
            self.assertEqual(saved[0]["text"], "결과를 내일까지 공유한다.")

    async def test_analyze_meeting_without_transcript_or_audio_still_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteMeetingRepository(Path(directory) / "meetings.sqlite3")
            repository.create(MeetingRecord("meeting-1", "user-1", "회의"))

            with self.assertRaises(ValueError):
                await build_analyze_meeting_workflow(repository=repository, analyzer=lambda text: None)(
                    request("analyze_meeting", {"meeting_id": "meeting-1"})
                )

    async def test_analyze_meeting_wraps_stt_failure_as_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteMeetingRepository(Path(directory) / "meetings.sqlite3")
            repository.create(MeetingRecord("meeting-1", "user-1", "회의"))
            repository.set_source_audio_uri("meeting-1", "user-1", "users/user-1/meetings/meeting-1/source/abc.wav")

            class FailingSttClient:
                def transcribe(self, *args, **kwargs):
                    raise SttProviderError("provider unavailable")

            with self.assertRaises(ValueError):
                await build_analyze_meeting_workflow(
                    repository=repository,
                    analyzer=lambda text: None,
                    stt_client_factory=lambda: FailingSttClient(),
                    storage_factory=lambda: type("S", (), {"get_object": lambda self, key: b"bytes"})(),
                )(request("analyze_meeting", {"meeting_id": "meeting-1"}))

    async def test_analyze_meeting_indexes_final_transcript_as_search_chunks(self) -> None:
        """분석 후 최종 Transcript가 Embedding과 함께 `meeting_chunks`에
        저장돼야 `search_meetings`가 근거를 찾을 수 있다(#23)."""

        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteMeetingRepository(Path(directory) / "meetings.sqlite3")
            repository.create(MeetingRecord("meeting-1", "user-1", "회의"))
            repository.save_transcript("meeting-1", "user-1", 0, "결과를 내일까지 공유한다.", True, 0, 1000)
            chunk_repository = SQLiteMeetingChunkRepository()

            result = await build_analyze_meeting_workflow(
                repository=repository,
                analyzer=lambda text: MeetingAnalysis(title="공유 일정 회의", summary="공유 일정을 확정했다.", action_items=[]),
                chunk_repository=chunk_repository,
                embedding_provider=FakeEmbeddingProvider(),
            )(request("analyze_meeting", {"meeting_id": "meeting-1"}))

            self.assertEqual(result.warnings, [])
            chunks = chunk_repository.list("meeting-1", "user-1")
            self.assertEqual(len(chunks), 1)
            self.assertEqual(chunks[0].content, "결과를 내일까지 공유한다.")
            self.assertEqual(chunks[0].started_at_ms, 0)
            self.assertEqual(chunks[0].ended_at_ms, 1000)
            self.assertEqual(len(chunks[0].embedding), 1536)

    async def test_analyze_meeting_threads_real_meeting_title_and_started_at_into_chunks(self) -> None:
        """색인되는 Chunk에 회의의 실제 제목·시각이 실려야
        `search_meetings`가 인용할 `meeting_date`를 만들 수 있다 — 예전엔
        `PostgresMeetingChunkRepository.create()`가 이 값을 받지 못해 항상
        `title="회의"`·`started_at=NULL`로 저장했고, 그래서 `meeting_started_at
        is None`인 인용 출처마다 `ValueError("meeting date is required for a
        cited source")`가 CORS 없는 500으로 샜다(2026-08-16, 14번 갭 문서 —
        실사용자 첫 `search_meetings` 실행에서 재현). `started_at`을 등록 시
        아무도 보내지 않아(`Meeting.tsx`) `created_at`으로 대신 채운다.

        `SQLiteMeetingChunkRepository`는 계약 테스트 전용이라 이 필드들을
        Chunk 테이블에 되돌려주지 않는다(Postgres에서만 부모 `meetings` 행에
        쓰인다) — 그래서 실제로 `create()`에 어떤 값이 전달되는지를 가로채는
        Spy Repository로 검증한다."""

        created_records: list[object] = []

        class SpyChunkRepository(SQLiteMeetingChunkRepository):
            def create(self, chunk):  # type: ignore[override]
                created_records.append(chunk)
                return super().create(chunk)

        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteMeetingRepository(Path(directory) / "meetings.sqlite3")
            repository.create(MeetingRecord("meeting-1", "user-1", "예산 회의"))
            saved = repository.get("meeting-1", "user-1")
            repository.save_transcript("meeting-1", "user-1", 0, "결과를 내일까지 공유한다.", True, 0, 1000)

            await build_analyze_meeting_workflow(
                repository=repository,
                analyzer=lambda text: MeetingAnalysis(title="공유 일정 회의", summary="공유 일정을 확정했다.", action_items=[]),
                chunk_repository=SpyChunkRepository(),
                embedding_provider=FakeEmbeddingProvider(),
            )(request("analyze_meeting", {"meeting_id": "meeting-1"}))

            self.assertEqual(len(created_records), 1)
            self.assertEqual(created_records[0].meeting_title, "예산 회의")
            self.assertIsNotNone(saved.created_at)
            self.assertEqual(created_records[0].meeting_started_at, saved.created_at)

    async def test_analyze_meeting_splits_long_single_row_transcript_by_sentence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteMeetingRepository(Path(directory) / "meetings.sqlite3")
            repository.create(MeetingRecord("meeting-1", "user-1", "회의"))
            long_text = " ".join(f"문장 {index}입니다." for index in range(60))
            repository.save_transcript("meeting-1", "user-1", 0, long_text, True)
            chunk_repository = SQLiteMeetingChunkRepository()

            await build_analyze_meeting_workflow(
                repository=repository,
                analyzer=lambda text: MeetingAnalysis(title="요약 회의", summary="요약", action_items=[]),
                chunk_repository=chunk_repository,
                embedding_provider=FakeEmbeddingProvider(),
            )(request("analyze_meeting", {"meeting_id": "meeting-1"}))

            chunks = chunk_repository.list("meeting-1", "user-1")
            self.assertGreater(len(chunks), 1)
            for chunk in chunks:
                self.assertLessEqual(len(chunk.content), 420)
                self.assertIsNone(chunk.started_at_ms)
            self.assertEqual("".join(chunk.content for chunk in chunks).replace(" ", ""), long_text.replace(" ", ""))

    async def test_analyze_meeting_degrades_to_a_warning_when_chunk_indexing_is_unavailable(self) -> None:
        """Postgres/Embedding Provider가 없어도 이미 계산된 분석 결과는
        그대로 반환하고, 색인 실패만 재시도 가능한 경고로 붙는다."""

        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteMeetingRepository(Path(directory) / "meetings.sqlite3")
            repository.create(MeetingRecord("meeting-1", "user-1", "회의"))
            repository.save_transcript("meeting-1", "user-1", 0, "결과를 내일까지 공유한다.", True)

            previous = __import__("os").environ.pop("DATABASE_URL", None)
            try:
                result = await build_analyze_meeting_workflow(
                    repository=repository,
                    analyzer=lambda text: MeetingAnalysis(title="요약 회의", summary="요약", action_items=[]),
                )(request("analyze_meeting", {"meeting_id": "meeting-1"}))
            finally:
                if previous is not None:
                    __import__("os").environ["DATABASE_URL"] = previous

            self.assertEqual(result.data["type"], "meeting_analysis")
            self.assertEqual(len(result.warnings), 1)
            self.assertEqual(result.warnings[0]["code"], "MEETING_CHUNK_INDEXING_UNAVAILABLE")

    async def test_search_meetings_returns_grounded_sources(self) -> None:
        result = await build_search_meetings_workflow(
            repository=FakeSearchRepository(), embedding_provider=FakeEmbeddingProvider()
        )(request("search_meetings", {"query": "예산", "limit": 5}))
        self.assertFalse(result.mock)
        self.assertEqual(result.data["type"], "grounded_answer")
        self.assertFalse(result.data["data"]["insufficient_evidence"])
        self.assertEqual(result.data["data"]["sources"][0]["meeting_chunk_id"], "chunk-1")


if __name__ == "__main__":
    unittest.main()
