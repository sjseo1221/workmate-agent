"""`app/audio.py`의 PCM16→WAV 병합 계약 테스트."""

import unittest
import wave
import io

from app.audio import STT_SAMPLE_RATE, wrap_pcm16_as_wav


class WrapPcm16AsWavTests(unittest.TestCase):
    def test_produces_a_readable_wav_file_with_correct_metadata(self):
        pcm_bytes = (100).to_bytes(2, "little", signed=True) * 4800  # 200ms @ 24kHz
        wav_bytes = wrap_pcm16_as_wav(pcm_bytes)
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            self.assertEqual(wav_file.getnchannels(), 1)
            self.assertEqual(wav_file.getsampwidth(), 2)
            self.assertEqual(wav_file.getframerate(), STT_SAMPLE_RATE)
            self.assertEqual(wav_file.getnframes(), 4800)
            self.assertEqual(wav_file.readframes(4800), pcm_bytes)

    def test_empty_input_still_produces_a_valid_header(self):
        wav_bytes = wrap_pcm16_as_wav(b"")
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            self.assertEqual(wav_file.getnframes(), 0)


if __name__ == "__main__":
    unittest.main()
