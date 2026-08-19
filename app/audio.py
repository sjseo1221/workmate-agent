"""실시간 녹음 Chunk를 원본 음성 파일로 병합하는 순수 함수.

07-technical-specification.md §9.3: 브라우저는 mono PCM16·24kHz·200ms 단위
Chunk를 보낸다. Chunk를 단순히 이어붙이기만 하면 컨테이너 헤더가 없는
재생·STT 불가능한 파일이 되므로, 병합 시 WAV(RIFF) 헤더를 씌운다
(14번 갭 문서 #20 작업계획 메모).
"""

from __future__ import annotations

import io
import wave

STT_SAMPLE_RATE = 24000
STT_SAMPLE_WIDTH_BYTES = 2  # 16bit
STT_CHANNELS = 1  # mono


def wrap_pcm16_as_wav(pcm_bytes: bytes, sample_rate: int = STT_SAMPLE_RATE) -> bytes:
    """Raw PCM16 바이트열에 WAV 헤더를 씌운 완성된 파일 바이트를 반환한다."""

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(STT_CHANNELS)
        wav_file.setsampwidth(STT_SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_bytes)
    return buffer.getvalue()
