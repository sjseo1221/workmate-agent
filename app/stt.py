"""OpenAI 호환 `/audio/transcriptions` STT Client.

`app/embeddings.py`의 `OpenAICompatibleEmbeddingClient`와 같은 원칙을 따른다 —
직접 import하는 패키지만 의존성으로 선언한다는 방침(07-technical-specification.md
§2.2)에 따라 `openai` SDK나 `requests`를 새로 추가하지 않고 표준 라이브러리
`urllib.request`로 multipart/form-data 요청을 직접 만든다.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import websockets

STT_MODEL = "gpt-4o-transcribe"
REALTIME_URL = "wss://api.openai.com/v1/realtime?intent=transcription"


class SttProviderError(RuntimeError):
    """STT Provider 설정 오류 또는 호출 실패."""


class OpenAICompatibleSttClient:
    """파일 기반 최종 STT 호출 Client. API Key는 로그·예외에 남기지 않는다."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.base_url = (base_url or os.getenv("STT_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("STT_API_KEY", "")
        self.model = model or os.getenv("STT_MODEL", STT_MODEL)
        self.timeout_seconds = timeout_seconds
        if not self.base_url or not self.api_key:
            raise SttProviderError("STT_BASE_URL and STT_API_KEY are required")

    def _endpoint(self) -> str:
        return self.base_url if self.base_url.endswith("/audio/transcriptions") else f"{self.base_url}/audio/transcriptions"

    @staticmethod
    def _build_multipart_body(*, boundary: str, model: str, language: str, filename: str, content_type: str, audio_bytes: bytes) -> bytes:
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())

        field("model", model)
        field("language", language)
        field("response_format", "json")
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n".encode()
        )
        parts.append(audio_bytes)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        return b"".join(parts)

    def transcribe(self, audio_bytes: bytes, *, filename: str = "audio.wav", content_type: str = "audio/wav", language: str = "ko") -> str:
        """원본 음성을 최종 Transcript 텍스트로 변환한다.

        Realtime Partial 자막과 달리 이 메서드는 확정된 원본 음성 전체를
        받아 한 번에 처리하는 파일 기반 최종 STT다(04-scenarios.md §3,
        07-technical-specification.md §9.3).
        """

        if not audio_bytes:
            raise SttProviderError("audio_bytes must not be empty")
        boundary = uuid4().hex
        body = self._build_multipart_body(boundary=boundary, model=self.model, language=language, filename=filename, content_type=content_type, audio_bytes=audio_bytes)
        request = Request(
            self._endpoint(),
            data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise SttProviderError("STT provider request failed") from exc
        try:
            text = str(payload["text"])
        except (KeyError, TypeError) as exc:
            raise SttProviderError("STT provider response is invalid") from exc
        return text


class RealtimeSttSession:
    """OpenAI Realtime Transcription WebSocket 세션 하나를 감싼다.

    화면용 임시 자막 전용이다(07-technical-specification.md §9.3: "실시간
    결과는 화면용 임시 자막으로만 사용하고, 녹음 종료 후 확정된 원본
    음성으로 timestamp가 포함된 최종 STT를 다시 수행한다") — 이 세션이
    만든 결과는 어디에도 최종 Transcript로 저장하지 않는다. 프로토콜은
    `stt-evaluation/run_streaming_eval.py`(2026-08-11 이전 평가에서 실
    계정으로 이미 검증됨)를 그대로 따른다: `session.update`로 Transcription
    세션을 열고, `input_audio_buffer.append`로 PCM16 Chunk를 그대로
    흘려보내고, 주기적으로 `input_audio_buffer.commit`을 보내 구간을
    끊는다.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        url: str | None = None,
        model: str | None = None,
        language: str = "ko",
        connect_timeout_seconds: float = 10.0,
    ) -> None:
        self.api_key = api_key or os.getenv("STT_API_KEY", "")
        self.url = url or os.getenv("STT_REALTIME_URL", REALTIME_URL)
        self.model = model or os.getenv("STT_MODEL", STT_MODEL)
        self.language = language
        self.connect_timeout_seconds = connect_timeout_seconds
        if not self.api_key:
            raise SttProviderError("STT_API_KEY is required")
        self._socket: websockets.ClientConnection | None = None

    async def connect(self) -> None:
        """Provider에 연결하고 Transcription 세션을 연다."""

        self._socket = await websockets.connect(
            self.url,
            additional_headers={"Authorization": f"Bearer {self.api_key}"},
            max_size=16 * 1024 * 1024,
            open_timeout=self.connect_timeout_seconds,
        )
        await self._socket.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "type": "transcription",
                        "audio": {
                            "input": {
                                "format": {"type": "audio/pcm", "rate": 24000},
                                "transcription": {"model": self.model, "language": self.language},
                            }
                        },
                    },
                }
            )
        )

    async def send_audio(self, audio_base64: str) -> None:
        """이미 Base64로 인코딩된 PCM16 Chunk를 그대로 전달한다.

        브라우저가 보낸 Chunk를 다시 디코딩·인코딩하지 않고 그대로
        흘려보낸다 — 이미 07 §9.3이 요구하는 mono PCM16 24kHz Chunk라
        Provider가 기대하는 형식과 같다.
        """

        assert self._socket is not None, "connect() must be called first"
        await self._socket.send(json.dumps({"type": "input_audio_buffer.append", "audio": audio_base64}))

    async def commit(self) -> None:
        """지금까지 보낸 오디오를 하나의 구간으로 확정해 Provider가 결과를 내게 한다."""

        assert self._socket is not None, "connect() must be called first"
        await self._socket.send(json.dumps({"type": "input_audio_buffer.commit"}))

    async def events(self) -> AsyncIterator[dict[str, object]]:
        """Provider가 보내는 원시 이벤트를 그대로 순회한다."""

        assert self._socket is not None, "connect() must be called first"
        async for raw in self._socket:
            yield json.loads(raw)

    async def close(self) -> None:
        if self._socket is not None:
            await self._socket.close()
            self._socket = None
