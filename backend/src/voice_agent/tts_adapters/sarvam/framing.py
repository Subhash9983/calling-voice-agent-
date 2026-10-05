"""Provider PCM chunks -> normalized 24 kHz mono 20 ms frames (docs/09 §11).

Sarvam's ``linear16`` stream delivers base64 ``audio/pcm`` chunks
(signed 16-bit little-endian, 24 kHz mono) whose sizes are not frame
aligned (observed live: 29,376 then 16,384-byte chunks). The framer carries
partial samples/frames across chunks and pads only the final partial frame
with silence. A ``RIFF``/WAVE header, if a chunk ever carries one, is
validated (PCM, mono, 24 kHz, 16-bit) and stripped; anything else is corrupt.
"""

from __future__ import annotations

import struct
from typing import Final

from voice_agent.contracts.audio import (
    BYTES_PER_SAMPLE,
    MILLISECONDS_PER_SECOND,
    RECOMMENDED_FRAME_MS,
    TTS_SAMPLE_RATE_HZ,
    AudioFrame,
)
from voice_agent.tts_adapters.sarvam.connection import SarvamErrorKind, SarvamTransportError

FRAME_BYTES: Final = TTS_SAMPLE_RATE_HZ * RECOMMENDED_FRAME_MS // MILLISECONDS_PER_SECOND * 2
WAV_MIN_HEADER: Final = 44
WAV_PCM_FORMAT: Final = 1
_FMT: Final = struct.Struct("<HHIIHH")


def _corrupt() -> SarvamTransportError:
    return SarvamTransportError(SarvamErrorKind.CORRUPT_AUDIO)


def strip_wav_header(chunk: bytes) -> bytes:
    """Return the PCM payload of a WAVE chunk after validating its format."""
    if len(chunk) < WAV_MIN_HEADER or chunk[8:12] != b"WAVE":
        raise _corrupt()
    position, fmt_ok = 12, False
    while position + 8 <= len(chunk):
        chunk_id = chunk[position : position + 4]
        size = int.from_bytes(chunk[position + 4 : position + 8], "little")
        body = position + 8
        if chunk_id == b"fmt ":
            fmt, channels, rate, _, _, bits = _FMT.unpack_from(chunk, body)
            expected = (WAV_PCM_FORMAT, 1, TTS_SAMPLE_RATE_HZ, BYTES_PER_SAMPLE * 8)
            if (fmt, channels, rate, bits) != expected:
                raise _corrupt()
            fmt_ok = True
        elif chunk_id == b"data":
            if not fmt_ok:
                raise _corrupt()
            return chunk[body:]
        position = body + size
    raise _corrupt()


class PcmFramer:
    """Stateful per-segment framer on an adapter-owned monotonic playback timeline."""

    def __init__(self, *, session_id: str, start_ms: int = 0) -> None:
        self._session_id = session_id
        self._pending = b""
        self._next_ms = start_ms
        self.frames = 0
        self.padded_bytes = 0

    @property
    def next_ms(self) -> int:
        return self._next_ms

    @property
    def audio_ms(self) -> int:
        return self.frames * RECOMMENDED_FRAME_MS

    def push(self, pcm: bytes) -> list[AudioFrame]:
        if pcm.startswith(b"RIFF"):
            pcm = strip_wav_header(pcm)
        data = self._pending + pcm
        whole = len(data) - len(data) % FRAME_BYTES
        self._pending = data[whole:]
        return [self._frame(data[i : i + FRAME_BYTES]) for i in range(0, whole, FRAME_BYTES)]

    def flush(self) -> list[AudioFrame]:
        """Emit the trailing partial frame padded with silence (whole samples only)."""
        tail = self._pending[: len(self._pending) - len(self._pending) % BYTES_PER_SAMPLE]
        self._pending = b""
        if not tail:
            return []
        self.padded_bytes += FRAME_BYTES - len(tail)
        return [self._frame(tail + bytes(FRAME_BYTES - len(tail)))]

    def _frame(self, pcm: bytes) -> AudioFrame:
        frame = AudioFrame(
            session_id=self._session_id,
            track_label="agent",
            sequence=self.frames,
            sample_rate_hz=TTS_SAMPLE_RATE_HZ,
            duration_ms=RECOMMENDED_FRAME_MS,
            captured_at_ms=self._next_ms,
            pcm=pcm,
        )
        self.frames += 1
        self._next_ms += RECOMMENDED_FRAME_MS
        return frame
