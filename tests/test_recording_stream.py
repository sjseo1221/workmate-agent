"""M3.2 WebSocket 순서·중복·Gap 계약 테스트."""

import base64
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.internal_chat import _authenticated_user_or_assignee
from app.main import app
from app.meeting_api import meeting_repository
from tests.fixtures.fake_r2 import FakeR2Storage
from tests.fixtures.fake_realtime_stt import fake_realtime_session_factory


class RecordingStreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["WORKMATE_MEETING_DB_PATH"] = os.path.join(cls.tmp.name, "meetings.sqlite3")
        # 실제 저장소(.runtime/recording-buffers)를 건드리지 않도록 임시
        # 디렉터리를 쓴다 — 2026-08-15 R2 연동 전에는 이 값 자체가 없었다.
        cls.buffer_dir = tempfile.TemporaryDirectory()
        os.environ["WORKMATE_RECORDING_BUFFER_DIR"] = cls.buffer_dir.name
        meeting_repository.cache_clear()
        app.dependency_overrides[_authenticated_user_or_assignee] = lambda: "user-a"
        cls.client = TestClient(app)
        cls.client.post("/api/v1/meetings", json={"meeting_id": "stream-1", "title": "실시간 회의"})
        cls.client.post("/api/v1/meetings", json={"meeting_id": "stream-2", "title": "Subprotocol 인증 회의"})

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(_authenticated_user_or_assignee, None)
        meeting_repository.cache_clear()
        os.environ.pop("WORKMATE_MEETING_DB_PATH", None)
        os.environ.pop("WORKMATE_RECORDING_BUFFER_DIR", None)
        cls.tmp.cleanup()
        cls.buffer_dir.cleanup()

    def test_ack_duplicate_and_gap(self):
        audio = base64.b64encode(b"pcm").decode()
        with self.client.websocket_connect("/api/v1/meetings/stream-1/recording-stream", headers={"Authorization": "Bearer test", "X-User-Id": "user-a"}) as ws:
            ws.send_json({"chunk_no": 0, "audio_base64": audio})
            self.assertEqual(ws.receive_json()["type"], "chunk_ack")
            ws.send_json({"chunk_no": 0, "audio_base64": audio})
            self.assertTrue(ws.receive_json()["duplicate"])
            ws.send_json({"chunk_no": 2, "audio_base64": audio})
            self.assertEqual(ws.receive_json()["code"], "CHUNK_GAP")

    def test_reconnect_restores_last_chunk_from_store(self):
        audio = base64.b64encode(b"pcm-1").decode()
        with self.client.websocket_connect("/api/v1/meetings/stream-1/recording-stream", headers={"Authorization": "Bearer test", "X-User-Id": "user-a"}) as ws:
            ws.send_json({"chunk_no": 1, "audio_base64": audio})
            self.assertFalse(ws.receive_json()["duplicate"])
        with self.client.websocket_connect("/api/v1/meetings/stream-1/recording-stream", headers={"Authorization": "Bearer test", "X-User-Id": "user-a"}) as ws:
            ws.send_json({"chunk_no": 2, "audio_base64": audio})
            self.assertFalse(ws.receive_json()["duplicate"])

    def test_partial_transcript_is_persisted_and_readable(self):
        response = self.client.get("/api/v1/meetings/stream-1/transcript")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any(item["is_final"] == 0 for item in response.json()))

    def test_header_credentials_still_take_priority_over_subprotocol(self):
        """헤더가 있으면 기존처럼 헤더 값을 그대로 사용한다."""

        audio = base64.b64encode(b"pcm-header").decode()
        with self.client.websocket_connect(
            "/api/v1/meetings/stream-2/recording-stream",
            headers={"Authorization": "Bearer test", "X-User-Id": "user-a"},
            subprotocols=["bearer.ignored", "user.ignored"],
        ) as ws:
            ws.send_json({"chunk_no": 0, "audio_base64": audio})
            self.assertFalse(ws.receive_json()["duplicate"])

    def test_subprotocol_credentials_authenticate_browser_clients(self):
        """헤더를 설정할 수 없는 브라우저 Client는 Subprotocol로 인증한다."""

        audio = base64.b64encode(b"pcm-browser").decode()
        with self.client.websocket_connect(
            "/api/v1/meetings/stream-2/recording-stream",
            subprotocols=["bearer.browser-token", "user.user-a"],
        ) as ws:
            # 서버가 제시된 Subprotocol 중 하나를 그대로 선택해 돌려줘야
            # 브라우저가 Handshake를 성공으로 인정한다 — 선택하지 않으면
            # 서버 로그에는 정상 accept로 보여도 실제 Chrome 등에서는
            # `error`/`close` 이벤트만 발생하는 "WebSocket 연결 오류"가
            # 재현된다(2026-08-15 실사용 중 발견, TestClient는 이 브라우저
            # 전용 규칙을 강제하지 않아 이전엔 잡히지 않았다).
            self.assertIn(ws.accepted_subprotocol, ["bearer.browser-token", "user.user-a"])
            ws.send_json({"chunk_no": 1, "audio_base64": audio})
            self.assertFalse(ws.receive_json()["duplicate"])

    def test_header_auth_leaves_subprotocol_unselected(self):
        """헤더 인증 경로는 애초에 Subprotocol을 제시하지 않으므로 선택할
        것도 없다 — `accept(subprotocol=None)`이 기존 동작과 같아야 한다."""

        self.client.post("/api/v1/meetings", json={"meeting_id": "stream-header-only", "title": "헤더 전용 인증 회의"})
        audio = base64.b64encode(b"pcm-header-2").decode()
        with self.client.websocket_connect(
            "/api/v1/meetings/stream-header-only/recording-stream",
            headers={"Authorization": "Bearer test", "X-User-Id": "user-a"},
        ) as ws:
            self.assertIsNone(ws.accepted_subprotocol)
            ws.send_json({"chunk_no": 0, "audio_base64": audio})
            self.assertFalse(ws.receive_json()["duplicate"])

    def test_session_end_merges_chunks_and_uploads_to_r2(self):
        """세션 종료 시 누적 Chunk가 WAV로 병합돼 R2(Fake)에 업로드되고
        Meeting.source_audio_uri가 확정돼야 한다 — 2026-08-15 이전엔 Chunk가
        해시 계산에만 쓰이고 버려져 이 전체가 없었다(#20)."""

        self.client.post("/api/v1/meetings", json={"meeting_id": "stream-finalize", "title": "병합 확인용 회의"})
        fake_r2 = FakeR2Storage()
        chunk_a = base64.b64encode(b"AAAA").decode()
        chunk_b = base64.b64encode(b"BBBB").decode()
        with patch("app.recording_stream.r2_storage", return_value=fake_r2):
            with self.client.websocket_connect(
                "/api/v1/meetings/stream-finalize/recording-stream",
                headers={"Authorization": "Bearer test", "X-User-Id": "user-a"},
            ) as ws:
                ws.send_json({"chunk_no": 0, "audio_base64": chunk_a})
                ws.receive_json()
                ws.send_json({"chunk_no": 1, "audio_base64": chunk_b})
                ws.receive_json()
            # `with` 블록을 벗어나며 정상 종료 → `finally`의 finalize가 실행됨

        self.assertEqual(len(fake_r2.objects), 1)
        (object_key,) = fake_r2.objects.keys()
        self.assertTrue(object_key.startswith("users/user-a/meetings/stream-finalize/source/"))
        self.assertTrue(object_key.endswith(".wav"))
        stored_bytes, content_type = fake_r2.objects[object_key]
        self.assertEqual(content_type, "audio/wav")
        # WAV 헤더(44바이트) + 원본 PCM 8바이트(AAAA+BBBB) 순서 보존 확인
        self.assertEqual(stored_bytes[-8:], b"AAAABBBB")

        meeting = meeting_repository().get("stream-finalize", "user-a")
        self.assertEqual(meeting.source_audio_uri, object_key)

    def test_finalize_is_silent_and_keeps_buffer_when_r2_not_configured(self):
        """R2 미설정 시에도 WebSocket 종료 자체는 실패하지 않아야 한다
        (사용자에게 이미 닫힌 연결로 오류를 알릴 방법이 없어 로그로만 남김)."""

        self.client.post("/api/v1/meetings", json={"meeting_id": "stream-no-r2", "title": "R2 미설정 회의"})
        audio = base64.b64encode(b"XYZ1").decode()
        with patch("app.recording_stream.r2_storage", side_effect=RuntimeError("R2 configuration is missing")):
            with self.client.websocket_connect(
                "/api/v1/meetings/stream-no-r2/recording-stream",
                headers={"Authorization": "Bearer test", "X-User-Id": "user-a"},
            ) as ws:
                ws.send_json({"chunk_no": 0, "audio_base64": audio})
                ws.receive_json()
        meeting = meeting_repository().get("stream-no-r2", "user-a")
        self.assertIsNone(meeting.source_audio_uri)

    def test_realtime_captions_are_forwarded_as_transcript_partial(self):
        """Provider가 구성돼 있으면 Delta·Final 이벤트를 그대로 자막으로
        중계해야 한다 — 2026-08-15 이전엔 이 경로 자체가 없어 자막이 항상
        빈 문자열이었다(#21 실시간 절반)."""

        self.client.post("/api/v1/meetings", json={"meeting_id": "stream-captions", "title": "실시간 자막 확인용 회의"})
        events = [
            {"type": "conversation.item.input_audio_transcription.delta", "delta": "안녕"},
            {"type": "conversation.item.input_audio_transcription.delta", "delta": "하세요"},
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "안녕하세요"},
        ]
        session_factory = fake_realtime_session_factory(events)
        audio = base64.b64encode(b"pcm-caption").decode()
        with patch("app.recording_stream.RealtimeSttSession", session_factory):
            with self.client.websocket_connect(
                "/api/v1/meetings/stream-captions/recording-stream",
                headers={"Authorization": "Bearer test", "X-User-Id": "user-a"},
            ) as ws:
                ws.send_json({"chunk_no": 0, "audio_base64": audio})
                # chunk_ack 1개 + 자막 이벤트 3개 = 총 4개. 자막은 별도
                # 백그라운드 Task가 보내 chunk_ack과의 정확한 순서는 보장되지
                # 않으므로 4개를 모두 받아 종류별로 나눠 확인한다.
                received = [ws.receive_json() for _ in range(4)]

        acks = [msg for msg in received if msg.get("type") == "chunk_ack"]
        captions = [msg for msg in received if msg.get("type") == "transcript.partial"]
        self.assertEqual(len(acks), 1)
        self.assertFalse(acks[0]["duplicate"])
        self.assertEqual([caption["text"] for caption in captions], ["안녕", "안녕하세요", "안녕하세요"])
        self.assertEqual(session_factory.instances[0].sent_audio, [audio])

    def test_realtime_captions_gracefully_degrade_when_provider_unavailable(self):
        """Provider 연결 실패는 자막만 비우고 녹음·ACK 자체는 그대로
        동작해야 한다."""

        self.client.post("/api/v1/meetings", json={"meeting_id": "stream-no-stt", "title": "STT 미설정 회의"})
        audio = base64.b64encode(b"pcm-no-stt").decode()

        class FailingRealtimeSession:
            async def connect(self):
                raise RuntimeError("provider unreachable")

        with patch("app.recording_stream.RealtimeSttSession", FailingRealtimeSession):
            with self.client.websocket_connect(
                "/api/v1/meetings/stream-no-stt/recording-stream",
                headers={"Authorization": "Bearer test", "X-User-Id": "user-a"},
            ) as ws:
                ws.send_json({"chunk_no": 0, "audio_base64": audio})
                ack = ws.receive_json()
        self.assertEqual(ack["type"], "chunk_ack")
        self.assertFalse(ack["duplicate"])

    def test_subprotocol_without_user_entry_is_rejected(self):
        """Subprotocol에 user 항목이 없으면 인증되지 않은 것으로 취급한다."""

        with self.assertRaises(Exception):
            with self.client.websocket_connect(
                "/api/v1/meetings/stream-2/recording-stream",
                subprotocols=["bearer.browser-token"],
            ) as ws:
                ws.receive_json()


if __name__ == "__main__":
    unittest.main()
