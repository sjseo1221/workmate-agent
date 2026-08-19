"""`RealtimeSttSession`과 같은 인터페이스의 실시간 자막 테스트 Double."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable


class FakeRealtimeSttSession:
    """실 Provider 연결 없이 미리 정해둔 이벤트를 그대로 재생한다."""

    def __init__(self, events: list[dict[str, object]] | None = None) -> None:
        self.sent_audio: list[str] = []
        self.connected = False
        self.closed = False
        self._pending_events = list(events or [])
        self._queue: asyncio.Queue[object] | None = None
        self._closed_sentinel = object()

    async def connect(self) -> None:
        self.connected = True
        self._queue = asyncio.Queue()
        for event in self._pending_events:
            self._queue.put_nowait(event)

    async def send_audio(self, audio_base64: str) -> None:
        self.sent_audio.append(audio_base64)

    async def commit(self) -> None:
        pass

    async def events(self) -> AsyncIterator[dict[str, object]]:
        assert self._queue is not None, "connect() must be called first"
        while True:
            event = await self._queue.get()
            if event is self._closed_sentinel:
                return
            yield event  # type: ignore[misc]

    def push_event(self, event: dict[str, object]) -> None:
        assert self._queue is not None, "connect() must be called first"
        self._queue.put_nowait(event)

    async def close(self) -> None:
        self.closed = True
        if self._queue is not None:
            await self._queue.put(self._closed_sentinel)


def fake_realtime_session_factory(events: list[dict[str, object]] | None = None) -> Callable[[], FakeRealtimeSttSession]:
    """`app.recording_stream.RealtimeSttSession` 자리에 그대로 patch할 Factory를 만든다.

    반환하는 함수의 `.instances`에 실제로 생성된 Fake Session들이 쌓여
    테스트가 `sent_audio` 등을 나중에 확인할 수 있다.
    """

    instances: list[FakeRealtimeSttSession] = []

    def factory(*_args: object, **_kwargs: object) -> FakeRealtimeSttSession:
        session = FakeRealtimeSttSession(events)
        instances.append(session)
        return session

    factory.instances = instances  # type: ignore[attr-defined]
    return factory
