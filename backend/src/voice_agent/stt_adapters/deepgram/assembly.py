"""Provider audio time -> capture timeline, and turn-window assembly (docs/07 §9, §11).

- :class:`StreamTimeMap`: Deepgram reports word/segment times as seconds of
  audio *sent on that stream attempt*. Each sent frame's stream offset and
  monotonic capture time are recorded (bounded), so provider times map back
  to the authoritative capture timeline even across microphone pauses.
- :class:`SegmentAssembler`: a bounded store of finalized (``is_final``)
  segments. A turn takes the words whose mapped audio overlaps its speech
  window, in audio-time order; words ending before the window are excluded
  (old window / queued echo), words starting after it stay for the next turn.
  Arrival order never decides ownership.
"""

from __future__ import annotations

import bisect
from collections import Counter, deque
from dataclasses import dataclass, replace
from typing import Final

from voice_agent.contracts.stt import AudioWindow

DEFAULT_MAP_FRAMES: Final = 6000  # two minutes of 20 ms frames
DEFAULT_MAX_SEGMENTS: Final = 200
MS_PER_SECOND: Final = 1000


@dataclass(frozen=True, slots=True)
class _SentFrame:
    offset_ms: int
    captured_at_ms: int
    duration_ms: int


class StreamTimeMap:
    def __init__(self, max_frames: int = DEFAULT_MAP_FRAMES) -> None:
        self._frames: deque[_SentFrame] = deque(maxlen=max_frames)
        self.sent_ms = 0

    def record(self, *, captured_at_ms: int, duration_ms: int) -> None:
        self._frames.append(_SentFrame(self.sent_ms, captured_at_ms, duration_ms))
        self.sent_ms += duration_ms

    def to_capture_ms(self, offset_s: float) -> int:
        offset = round(offset_s * MS_PER_SECOND)
        if not self._frames:
            return offset
        first, last = self._frames[0], self._frames[-1]
        if offset >= self.sent_ms:
            return last.captured_at_ms + last.duration_ms
        if offset < first.offset_ms:
            return max(first.captured_at_ms - (first.offset_ms - offset), 0)
        offsets = [frame.offset_ms for frame in self._frames]
        frame = self._frames[bisect.bisect_right(offsets, offset) - 1]
        return frame.captured_at_ms + (offset - frame.offset_ms)


@dataclass(frozen=True, slots=True)
class MappedWord:
    text: str
    start_ms: int
    end_ms: int
    language: str | None = None


@dataclass(frozen=True, slots=True)
class Segment:
    start_ms: int
    end_ms: int
    text: str
    words: tuple[MappedWord, ...] = ()
    languages: tuple[str, ...] = ()
    confidence: float | None = None

    @property
    def fingerprint(self) -> tuple[int, int, str]:
        return (self.start_ms, self.end_ms, " ".join(self.text.split()))


@dataclass(frozen=True, slots=True)
class TakenTranscript:
    segments: tuple[Segment, ...]
    text: str
    language: str | None
    confidence: float | None


@dataclass(frozen=True, slots=True)
class _Split:
    kept: Segment | None
    later: Segment | None
    excluded_words: int


def _split(segment: Segment, window: AudioWindow) -> _Split:
    if not segment.words:
        if segment.end_ms <= window.start_ms:
            return _Split(None, None, 0)
        if segment.start_ms >= window.end_ms:
            return _Split(None, segment, 0)
        return _Split(segment, None, 0)
    excluded = [w for w in segment.words if w.end_ms <= window.start_ms]
    later = [w for w in segment.words if w.start_ms >= window.end_ms]
    kept = [w for w in segment.words if w not in excluded and w not in later]
    return _Split(_part(segment, kept), _part(segment, later), len(excluded))


def _part(segment: Segment, words: list[MappedWord]) -> Segment | None:
    if not words:
        return None
    if len(words) == len(segment.words):
        return segment
    return replace(
        segment,
        start_ms=words[0].start_ms,
        end_ms=words[-1].end_ms,
        text=" ".join(w.text for w in words),
        words=tuple(words),
    )


def _language(segments: tuple[Segment, ...]) -> str | None:
    counts = Counter(w.language for s in segments for w in s.words if w.language)
    if counts:
        return counts.most_common(1)[0][0]
    return next((s.languages[0] for s in segments if s.languages), None)


def _confidence(segments: tuple[Segment, ...]) -> float | None:
    values = [s.confidence for s in segments if s.confidence is not None]
    return sum(values) / len(values) if values else None


class SegmentAssembler:
    def __init__(self, max_segments: int = DEFAULT_MAX_SEGMENTS) -> None:
        self._max = max_segments
        self._pending: list[Segment] = []
        self._seen: deque[tuple[int, int, str]] = deque(maxlen=max_segments)
        self.duplicates = 0
        self.overflowed = 0
        self.excluded_words = 0

    def add(self, segment: Segment) -> bool:
        """Store a finalized segment; ``False`` for a duplicate (same interval and text)."""
        if segment.fingerprint in self._seen:
            self.duplicates += 1
            return False
        self._seen.append(segment.fingerprint)
        if not segment.text.strip() and not segment.words:
            return True
        self._pending.append(segment)
        self._pending.sort(key=lambda s: (s.start_ms, s.end_ms))
        while len(self._pending) > self._max:
            self._pending.pop(0)
            self.overflowed += 1
        return True

    def pending_text(self) -> str:
        return " ".join(s.text for s in self._pending if s.text.strip())

    def take(self, window: AudioWindow | None) -> TakenTranscript:
        """Remove and return the words bound to ``window`` (all pending when ``None``)."""
        if window is None:
            kept, self._pending = tuple(self._pending), []
        else:
            kept = self._take_window(window)
        text = " ".join(s.text.strip() for s in kept if s.text.strip())
        if not text:
            return TakenTranscript(segments=kept, text="", language=None, confidence=None)
        return TakenTranscript(
            segments=kept, text=text, language=_language(kept), confidence=_confidence(kept)
        )

    def _take_window(self, window: AudioWindow) -> tuple[Segment, ...]:
        kept: list[Segment] = []
        remaining: list[Segment] = []
        for segment in self._pending:
            split = _split(segment, window)
            self.excluded_words += split.excluded_words
            if split.kept is not None:
                kept.append(split.kept)
            if split.later is not None:
                remaining.append(split.later)
        self._pending = remaining
        return tuple(kept)
