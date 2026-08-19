"""`app/stt.py` OpenAI 호환 STT Client 계약 테스트.

실제 Provider를 호출하지 않는다 — `urlopen`만 Fake로 바꿔 요청 조립·응답
파싱 경계를 검증한다. 실제 연결 자체는 2026-08-15
`stt-evaluation/.env`(검증된 실 계정)로 수동 확인했다.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import AsyncMock, patch
from urllib.error import URLError

from app.stt import OpenAICompatibleSttClient, RealtimeSttSession, SttProviderError


class FakeHttpResponse:
    def __init__(self, body: dict) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class OpenAICompatibleSttClientTests(unittest.TestCase):
    def setUp(self):
        self._previous = {name: os.environ.get(name) for name in ("STT_BASE_URL", "STT_API_KEY", "STT_MODEL")}
        os.environ["STT_BASE_URL"] = "https://stt.example.invalid/v1"
        os.environ["STT_API_KEY"] = "test-key"
        os.environ.pop("STT_MODEL", None)

    def tearDown(self):
        for name, value in self._previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_missing_config_is_rejected(self):
        os.environ.pop("STT_BASE_URL", None)
        with self.assertRaises(SttProviderError):
            OpenAICompatibleSttClient()

    def test_empty_audio_is_rejected_without_a_network_call(self):
        client = OpenAICompatibleSttClient()
        with self.assertRaises(SttProviderError):
            client.transcribe(b"")

    def test_transcribe_parses_text_from_response(self):
        client = OpenAICompatibleSttClient()
        with patch("app.stt.urlopen", return_value=FakeHttpResponse({"text": "오늘 회의를 시작합니다."})) as mock_urlopen:
            text = client.transcribe(b"pcm-bytes", filename="a.wav", content_type="audio/wav")
        self.assertEqual(text, "오늘 회의를 시작합니다.")
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "https://stt.example.invalid/v1/audio/transcriptions")
        self.assertIn("multipart/form-data; boundary=", request.headers["Content-type"])
        self.assertEqual(request.headers["Authorization"], "Bearer test-key")
        self.assertIn(b"pcm-bytes", request.data)
        self.assertIn(b'filename="a.wav"', request.data)

    def test_network_failure_is_wrapped(self):
        client = OpenAICompatibleSttClient()
        with patch("app.stt.urlopen", side_effect=URLError("boom")):
            with self.assertRaises(SttProviderError):
                client.transcribe(b"pcm-bytes")

    def test_malformed_response_is_rejected(self):
        client = OpenAICompatibleSttClient()
        with patch("app.stt.urlopen", return_value=FakeHttpResponse({"unexpected": "shape"})):
            with self.assertRaises(SttProviderError):
                client.transcribe(b"pcm-bytes")


class FakeRealtimeSocket:
    """`websockets.connect()`가 반환하는 연결 객체의 최소 Double."""

    def __init__(self, incoming: list[dict]) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._incoming = [json.dumps(event) for event in incoming]

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)


class RealtimeSttSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._previous = os.environ.get("STT_API_KEY")
        os.environ["STT_API_KEY"] = "test-key"

    def tearDown(self):
        if self._previous is None:
            os.environ.pop("STT_API_KEY", None)
        else:
            os.environ["STT_API_KEY"] = self._previous

    def test_missing_api_key_is_rejected_before_any_network_call(self):
        os.environ.pop("STT_API_KEY", None)
        with self.assertRaises(SttProviderError):
            RealtimeSttSession()

    async def test_connect_opens_a_transcription_session(self):
        fake_socket = FakeRealtimeSocket([])
        with patch("app.stt.websockets.connect", AsyncMock(return_value=fake_socket)) as mock_connect:
            session = RealtimeSttSession(language="ko")
            await session.connect()
        self.assertEqual(mock_connect.call_args.args[0], RealtimeSttSession().url)
        self.assertEqual(mock_connect.call_args.kwargs["additional_headers"], {"Authorization": "Bearer test-key"})
        sent = json.loads(fake_socket.sent[0])
        self.assertEqual(sent["type"], "session.update")
        self.assertEqual(sent["session"]["audio"]["input"]["format"], {"type": "audio/pcm", "rate": 24000})
        self.assertEqual(sent["session"]["audio"]["input"]["transcription"]["language"], "ko")

    async def test_send_audio_and_commit_use_the_provider_message_shapes(self):
        fake_socket = FakeRealtimeSocket([])
        with patch("app.stt.websockets.connect", AsyncMock(return_value=fake_socket)):
            session = RealtimeSttSession()
            await session.connect()
            await session.send_audio("YmFzZTY0")
            await session.commit()
        self.assertEqual(json.loads(fake_socket.sent[1]), {"type": "input_audio_buffer.append", "audio": "YmFzZTY0"})
        self.assertEqual(json.loads(fake_socket.sent[2]), {"type": "input_audio_buffer.commit"})

    async def test_events_parses_each_incoming_message_as_json(self):
        incoming = [
            {"type": "conversation.item.input_audio_transcription.delta", "delta": "안"},
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "안녕"},
        ]
        fake_socket = FakeRealtimeSocket(incoming)
        with patch("app.stt.websockets.connect", AsyncMock(return_value=fake_socket)):
            session = RealtimeSttSession()
            await session.connect()
            received = [event async for event in session.events()]
        self.assertEqual(received, incoming)

    async def test_close_closes_the_underlying_socket(self):
        fake_socket = FakeRealtimeSocket([])
        with patch("app.stt.websockets.connect", AsyncMock(return_value=fake_socket)):
            session = RealtimeSttSession()
            await session.connect()
            await session.close()
        self.assertTrue(fake_socket.closed)


if __name__ == "__main__":
    unittest.main()
