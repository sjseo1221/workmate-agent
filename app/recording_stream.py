"""회의 실시간 녹음 WebSocket의 순서·중복·Gap 계약."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.audio import wrap_pcm16_as_wav
from app.meeting_api import meeting_repository, r2_storage
from app.stt import RealtimeSttSession, SttProviderError

logger = logging.getLogger("workmate-agent.recording-stream")

router = APIRouter(tags=["meeting-recording"])

_BUFFER_DIR_ENV = "WORKMATE_RECORDING_BUFFER_DIR"


def _buffer_path(meeting_id: str, user_id: str) -> Path:
    """세션 동안 받은 원본 PCM16 바이트를 임시로 쌓아두는 파일 경로.

    Chunk 자체는 어디에도 저장되지 않고 버려지던 게 원래 문제였다(2026-08-15
    발견, 14번 갭 문서 #20). 이 파일은 재연결에도 살아남도록 `meeting_id +
    user_id`로 고정 경로를 쓴다 — 새 WebSocket 연결(=새 함수 호출)이어도
    이어서 추가(append)할 수 있다. 세션이 끝나면 병합·업로드 후 삭제한다.
    """

    base = Path(os.getenv(_BUFFER_DIR_ENV, ".runtime/recording-buffers"))
    directory = base / user_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{meeting_id}.pcm"


def _credentials(websocket: WebSocket) -> tuple[str, str, str | None]:
    """`Authorization`/`X-User-Id` 헤더 또는 WebSocket Subprotocol에서
    자격 증명을 읽는다.

    브라우저의 `WebSocket` API는 Handshake에 임의의 요청 헤더를 설정할 수
    없어, 헤더가 없는 요청은 `Sec-WebSocket-Protocol`로 전달된
    `bearer.<token>`/`user.<user_id>` 항목을 대신 사용한다. 두 경로 모두
    존재 여부만 확인하는 기존 계약과 같은 수준의 검증이다.

    반환하는 세 번째 값은 `websocket.accept(subprotocol=...)`에 그대로
    돌려줄 값이다 — 이전엔 "WS 표준상 Subprotocol을 선택하지 않아도
    Handshake는 성공한다"고 보고 echo하지 않았는데, 이는 RFC 6455 자체는
    맞지만 **실제 브라우저(Chrome 등)는 클라이언트가 Subprotocol을
    제시했는데 서버 응답에 그중 하나가 선택돼 있지 않으면 Handshake 자체를
    실패로 처리한다** — 서버 로그엔 정상 accept로 남지만 브라우저는
    `error`/`close` 이벤트만 던져 원인을 알 수 없는 "WebSocket 연결
    오류"로 보인다(2026-08-15 실사용 중 재현·확인). Starlette
    `TestClient`와 순수 `websockets` Python Client는 이 브라우저 전용
    규칙을 강제하지 않아 기존 테스트로는 잡히지 않았다.
    """

    authorization = websocket.headers.get("authorization", "")
    user_id = websocket.headers.get("x-user-id", "")
    if authorization.startswith("Bearer ") and user_id:
        return authorization, user_id, None
    offered = [item.strip() for item in websocket.headers.get("sec-websocket-protocol", "").split(",") if item.strip()]
    token = next((item[len("bearer."):] for item in offered if item.startswith("bearer.")), "")
    subprotocol_user_id = next((item[len("user."):] for item in offered if item.startswith("user.")), "")
    if token and subprotocol_user_id:
        # 클라이언트가 제시한 Subprotocol 중 하나를 그대로 돌려줘야
        # 브라우저가 Handshake를 성공으로 판단한다 — 값 자체의 의미는
        # 없고(자격 증명은 이미 위에서 파싱함) 협상 규칙만 만족시키면 된다.
        return f"Bearer {token}", subprotocol_user_id, offered[0]
    return "", "", None


async def _start_realtime_session() -> RealtimeSttSession | None:
    """가능하면 실시간 자막용 Provider 세션을 연다.

    STT_API_KEY가 없거나 Provider 연결에 실패해도 녹음·저장 자체는 이
    자막 기능과 독립적으로 계속돼야 한다 — R2/Provider 미설정 시 조용히
    자막 없이 진행하는 기존 방침(#20)과 같은 원칙이다.
    """

    try:
        session = RealtimeSttSession()
    except SttProviderError as exc:
        logger.info("realtime_stt_not_configured error=%s", exc)
        return None
    try:
        await session.connect()
    except Exception as exc:  # noqa: BLE001 - Provider 연결 실패를 자막 없음으로 흡수한다.
        logger.warning("realtime_stt_connect_failed error=%s", exc)
        return None
    return session


async def _forward_realtime_captions(websocket: WebSocket, realtime: RealtimeSttSession, send_lock: asyncio.Lock) -> None:
    """Provider의 Partial/Final 이벤트를 화면 자막으로 그대로 중계한다.

    Provider가 자체 Turn 검출(서버 VAD)로 문장이 끝날 때마다
    `conversation.item.input_audio_transcription.completed`를 보내므로
    별도로 주기적인 `input_audio_buffer.commit`을 보낼 필요가 없다
    (2026-08-15 실 계정으로 확인). 이 결과는 화면 표시 전용이라 어디에도
    최종 Transcript로 저장하지 않는다 — 녹음이 끝난 뒤 확정된 원본
    음성으로 다시 수행하는 파일 기반 최종 STT만 `is_final=True`로
    저장한다(07-technical-specification.md §9.3).
    """

    accumulated = ""
    caption_no = 0
    try:
        async for event in realtime.events():
            kind = str(event.get("type", ""))
            if kind.endswith(".delta") and "transcription" in kind:
                accumulated += str(event.get("delta", ""))
                async with send_lock:
                    await websocket.send_json({"type": "transcript.partial", "chunk_no": caption_no, "text": accumulated})
            elif kind == "conversation.item.input_audio_transcription.completed":
                text = str(event.get("transcript") or accumulated)
                async with send_lock:
                    await websocket.send_json({"type": "transcript.partial", "chunk_no": caption_no, "text": text})
                accumulated = ""
                caption_no += 1
            elif kind.endswith(".failed") or kind == "error":
                logger.warning("realtime_stt_provider_error meeting_error=%s", event)
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 자막 스트림 오류로 녹음 자체를 끊지 않는다.
        logger.warning("realtime_stt_stream_error error=%s", exc)


@router.websocket("/api/v1/meetings/{meeting_id}/recording-stream")
async def recording_stream(websocket: WebSocket, meeting_id: str) -> None:
    """인증된 사용자 세션의 JSON 음성 Chunk를 ACK하고 순서를 복구한다."""
    authorization, user_id, subprotocol = _credentials(websocket)
    if not authorization.startswith("Bearer ") or not user_id:
        await websocket.close(code=4401)
        return
    if meeting_repository().get(meeting_id, user_id) is None:
        await websocket.close(code=4404)
        return
    await websocket.accept(subprotocol=subprotocol)
    repository = meeting_repository()
    last, hashes = repository.session(meeting_id, user_id)
    buffer_path = _buffer_path(meeting_id, user_id)
    send_lock = asyncio.Lock()
    realtime = await _start_realtime_session()
    caption_task = asyncio.create_task(_forward_realtime_captions(websocket, realtime, send_lock)) if realtime else None
    try:
        while True:
            payload = await websocket.receive_json()
            chunk_no = payload.get("chunk_no")
            encoded = payload.get("audio_base64")
            if not isinstance(chunk_no, int) or chunk_no < 0 or not isinstance(encoded, str):
                async with send_lock:
                    await websocket.send_json({"type": "error", "code": "INVALID_CHUNK"})
                continue
            try:
                raw = base64.b64decode(encoded, validate=True)
            except ValueError:
                async with send_lock:
                    await websocket.send_json({"type": "error", "code": "INVALID_AUDIO"})
                continue
            digest = hashlib.sha256(raw).hexdigest()
            if str(chunk_no) in hashes:
                async with send_lock:
                    await websocket.send_json({"type": "chunk_ack", "chunk_no": chunk_no, "duplicate": True})
                continue
            expected = last + 1
            if chunk_no > expected:
                async with send_lock:
                    await websocket.send_json({"type": "error", "code": "CHUNK_GAP", "expected_chunk_no": expected})
                continue
            hashes[str(chunk_no)] = digest
            last = chunk_no
            repository.save_session(meeting_id, user_id, last, hashes)
            # 승인된 Chunk만(순서·중복 검사 통과) 원본 음성 버퍼에 이어붙인다 —
            # 이 시점 이후에는 항상 시간 순서가 보장된다.
            with buffer_path.open("ab") as buffer_file:
                buffer_file.write(raw)
            async with send_lock:
                await websocket.send_json({"type": "chunk_ack", "chunk_no": chunk_no, "duplicate": False})
            repository.save_transcript(meeting_id, user_id, chunk_no, "", False)
            if realtime is not None:
                try:
                    await realtime.send_audio(encoded)
                except Exception as exc:  # noqa: BLE001 - 자막 전송 실패로 녹음 자체를 끊지 않는다.
                    logger.warning("realtime_stt_send_failed meeting_id=%s error=%s", meeting_id, exc)
    except WebSocketDisconnect:
        return
    finally:
        if caption_task is not None:
            caption_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await caption_task
        if realtime is not None:
            await realtime.close()
        await _finalize_recording_session(buffer_path, meeting_id, user_id)


async def _finalize_recording_session(buffer_path: Path, meeting_id: str, user_id: str) -> None:
    """세션 종료(정상 종료 또는 접속 끊김) 시 누적된 PCM16을 WAV로 병합해
    R2에 업로드하고 `Meeting.source_audio_uri`를 확정한다.

    07-technical-specification.md §13.1: "Chunk마다 개별 객체로 저장하지
    않고 세션 종료 후 하나의 음성 파일로 병합해 업로드한다." R2가 설정되지
    않았거나 업로드가 실패해도 WebSocket 자체는 이미 끝난 뒤라 사용자에게
    직접 알릴 방법이 없다 — 로그로 남기고 버퍼 파일은 재시도할 수 있게
    지우지 않는다(운영 전환 시 재시도 Job으로 정리해야 하는 한계로 남긴다).
    """

    if not buffer_path.exists() or buffer_path.stat().st_size == 0:
        return
    pcm_bytes = buffer_path.read_bytes()
    wav_bytes = wrap_pcm16_as_wav(pcm_bytes)
    object_key = f"users/{user_id}/meetings/{meeting_id}/source/{hashlib.sha256(wav_bytes).hexdigest()}.wav"
    try:
        storage = r2_storage()
        storage.put_object(object_key, wav_bytes, "audio/wav")
    except RuntimeError as exc:
        logger.warning("recording_finalize_r2_unconfigured meeting_id=%s error=%s", meeting_id, exc)
        return
    except Exception as exc:  # noqa: BLE001 - R2 업로드 실패를 세션 종료 흐름에서 통째로 잡아 로그로만 남긴다.
        logger.warning("recording_finalize_upload_failed meeting_id=%s error=%s", meeting_id, exc)
        return
    meeting_repository().set_source_audio_uri(meeting_id, user_id, object_key)
    buffer_path.unlink(missing_ok=True)
    logger.info("recording_finalized meeting_id=%s user_id=%s object_key=%s bytes=%s", meeting_id, user_id, object_key, len(wav_bytes))
