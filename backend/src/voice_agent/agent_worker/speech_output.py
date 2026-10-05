"""Speech output for one authorized turn: delivered segments -> TTS -> agent audio (WP9).

Pipeline (docs/09 §6, §12-§13; docs/05 §8, §14; docs/06 §13):

``enqueue`` (TTS text preparation, bounded queue of five pieces, backpressure)
-> synthesis producer (:class:`SegmentSynthesizer`, one piece at a time)
-> bounded frame queue -> playback consumer -> ``SessionTransportPort``
(the WP6 LiveKit ``agent-audio`` source with its 200 ms ``AudioSource`` queue).

- the consumer is the final authorization point: every frame re-checks the
  conversation gate's generation fence before publication; stale frames are
  counted and dropped, never played;
- each piece is one playback segment with the WP6 ack identity
  (``worker_generation``, ``cancellation_generation``, ``segment_id``),
  announced on ``va.playback.v1`` as ``started`` before its first frame and
  ``completed`` after its audio played out (``wait_for_playout``);
  browser acknowledgements are evidence only;
- :meth:`cancel` runs after the gate advanced the fence (interruption step 2):
  cancel TTS, drop the application frame queue, ``clear_playback()``
  (``AudioSource.clear_queue()``), then tell the browser ``cancelled`` for
  the piece that was playing;
- a piece that finally fails stops the rest of the turn's speech (no
  sentence is skipped silently, no delivered audio is replayed);
- durable evidence and gate callbacks run through ordered background
  writers, never on the audio path.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Final

from pydantic import JsonValue

from voice_agent.agent_worker.ordered_writer import OrderedWriter
from voice_agent.agent_worker.realtime_publisher import RealtimePublisher
from voice_agent.agent_worker.speech_synthesis import SegmentSynthesizer, SpeechSetup
from voice_agent.agent_worker.speech_tracks import (
    DrainItem,
    EndItem,
    FrameItem,
    PlaybackItem,
    SegmentTrack,
    SpeechOutcome,
)
from voice_agent.agent_worker.stt_evidence import SttEvidence
from voice_agent.contracts.events import EventType
from voice_agent.contracts.failures import NormalizedFailure
from voice_agent.contracts.identity import GenerationStamp, PlaybackAckIdentity
from voice_agent.contracts.transport import PlaybackAck, PlaybackAckKind, PlaybackFrame
from voice_agent.orchestration.generations import FenceVerdict, GenerationFence
from voice_agent.orchestration.response_generation import DeliveredSegment
from voice_agent.ports.clock import Clock, IdGenerator
from voice_agent.ports.transport import SessionTransportPort
from voice_agent.ports.tts import TTSPort
from voice_agent.response_segmentation.tts_text import prepare_tts_text

MS_PER_SECOND: Final = 1000
DRAIN_LIMIT_S: Final = 180.0
Callback = Callable[[], Awaitable[None]]
AudibleCallback = Callable[[DeliveredSegment], Awaitable[None]]


async def _nothing() -> None:
    return None


async def _nothing_for(_segment: DeliveredSegment) -> None:
    return None


@dataclass(frozen=True, slots=True)
class SpeechDeps:
    setup: SpeechSetup
    tts: TTSPort
    transport: SessionTransportPort
    fence: GenerationFence
    evidence: SttEvidence
    publisher: RealtimePublisher
    clock: Clock
    ids: IdGenerator
    jitter: Callable[[], float] = field(default=random.random)


class SpeechTurn:
    def __init__(
        self,
        deps: SpeechDeps,
        *,
        turn_stamp: GenerationStamp,
        on_first_audio: Callback = _nothing,
        on_audible: AudibleCallback = _nothing_for,
    ) -> None:
        if turn_stamp.turn_id is None:
            raise ValueError("speech output needs the authorized turn stamp")
        setup = deps.setup
        self._deps = deps
        self._stamp = turn_stamp
        self._turn_id = turn_stamp.turn_id
        self._on_first_audio = on_first_audio
        self._on_audible = on_audible
        self._segments: asyncio.Queue[SegmentTrack | DrainItem] = asyncio.Queue(
            setup.max_queued_segments
        )
        self._frames: asyncio.Queue[PlaybackItem] = asyncio.Queue(setup.max_buffered_frames)
        self._writer = OrderedWriter()
        self._side = OrderedWriter()
        self._synth = SegmentSynthesizer(
            setup,
            tts=deps.tts,
            fence=deps.fence,
            evidence=deps.evidence,
            writer=self._writer,
            clock=deps.clock,
            ids=deps.ids,
            put=self._frames.put,
            jitter=deps.jitter,
        )
        self.tracks: list[SegmentTrack] = []
        self._drains: dict[int, asyncio.Event] = {}
        self._drain_seq = 0
        self._acks = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._started_ms = deps.clock.monotonic_ms()
        self._first_audio_ms: int | None = None
        self._failure: NormalizedFailure | None = None
        self._playing: SegmentTrack | None = None
        self._audible: set[int] = set()
        self._cancelled = False
        self._closed = False
        self.unspeakable_segments = 0
        # Monotonic time ``clear_playback()`` (``AudioSource.clear_queue()``)
        # returned after a cancel: server-side audible silence (WP10 evidence).
        self.cleared_at_ms: int | None = None

    # ------------------------------------------------------------ intake --
    def start(self) -> None:
        if not self._tasks:
            self._tasks = [
                asyncio.ensure_future(self._produce()),
                asyncio.ensure_future(self._consume()),
            ]

    async def enqueue(self, segment: DeliveredSegment) -> None:
        """Prepare and queue one delivered segment (blocks while five pieces wait)."""
        self.start()
        plan = prepare_tts_text(segment.text, segment.language_code)
        if plan.is_empty:
            self.unspeakable_segments += 1
            return
        for piece in plan.pieces:
            identity = PlaybackAckIdentity(
                worker_generation=self._stamp.worker_generation,
                cancellation_generation=self._stamp.cancellation_generation,
                segment_id=self._deps.ids.new_id(),
            )
            track = SegmentTrack(
                index=len(self.tracks),
                segment=segment,
                text=piece,
                stamp=self._stamp,
                identity=identity,
                normalization_version=plan.version,
            )
            self.tracks.append(track)
            await self._segments.put(track)

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def audible(self) -> bool:
        """Some piece of this turn's audio reached playback."""
        return any(track.playback_started for track in self.tracks)

    def _current(self) -> bool:
        return self._deps.fence.check(self._stamp) is FenceVerdict.ACCEPTED

    # ---------------------------------------------------------- producer --
    async def _produce(self) -> None:
        while True:
            item = await self._segments.get()
            if isinstance(item, DrainItem):
                await self._frames.put(item)
                continue
            if self._cancelled or self._failure is not None or not self._current():
                item.skipped = True
                continue
            await self._synth.synthesize(item)
            if item.failure is not None:
                self._failure = item.failure

    # ---------------------------------------------------------- consumer --
    async def _consume(self) -> None:
        while True:
            item = await self._frames.get()
            if isinstance(item, DrainItem):
                await self._side.flush()
                self._release(item)
                continue
            if self._deps.fence.check(item.stamp) is not FenceVerdict.ACCEPTED:
                if isinstance(item, FrameItem):
                    item.track.stale_frames += 1
                continue
            if isinstance(item, FrameItem):
                await self._play(item)
            elif isinstance(item, EndItem):
                await self._end(item.track)

    async def _play(self, item: FrameItem) -> None:
        track = item.track
        if not track.playback_started:
            await self._start_playback(track)
        frame = PlaybackFrame(identity=track.identity, turn_id=self._turn_id, frame=item.frame)
        await self._deps.transport.publish_audio(frame)

    async def _start_playback(self, track: SegmentTrack) -> None:
        track.playback_started = True
        self._playing = track
        await self._deps.publisher.publish_playback(
            "started", track.identity, EventType.PLAYBACK_STARTED, turn_id=self._turn_id
        )
        if self._first_audio_ms is None:
            self._first_audio_ms = self._deps.clock.monotonic_ms() - self._started_ms
            self._side.submit(self._on_first_audio)
        if id(track.segment) not in self._audible:
            self._audible.add(id(track.segment))
            segment = track.segment
            self._side.submit(lambda: self._on_audible(segment))
        self._record(EventType.PLAYBACK_STARTED, track)

    async def _end(self, track: SegmentTrack) -> None:
        transport = self._deps.transport
        await transport.finish_segment(track.identity)
        await transport.wait_for_playout()
        if not self._current():
            return
        track.playback_completed = True
        if self._playing is track:
            self._playing = None
        await self._deps.publisher.publish_playback(
            "completed", track.identity, EventType.PLAYBACK_COMPLETED, turn_id=self._turn_id
        )
        self._record(EventType.PLAYBACK_COMPLETED, track)

    def _record(self, event_type: EventType, track: SegmentTrack) -> None:
        payload: dict[str, JsonValue] = {
            "segment_sequence": track.segment.sequence,
            "piece_index": track.index,
        }
        if event_type is EventType.PLAYBACK_STARTED and track.index == 0:
            payload["response_to_first_audio_ms"] = self._first_audio_ms or 0
        evidence, turn_id = self._deps.evidence, self._turn_id
        self._writer.submit(lambda: evidence.event(event_type, turn_id=turn_id, payload=payload))

    # -------------------------------------------------------------- drain --
    def _release(self, item: DrainItem) -> None:
        event = self._drains.pop(item.token, None)
        if event is not None:
            event.set()

    async def drain(self) -> SpeechOutcome:
        """Wait until everything queued so far played out (or was dropped)."""
        if self._tasks and not self._closed:
            self._drain_seq += 1
            token = self._drain_seq
            event = self._drains[token] = asyncio.Event()
            await self._segments.put(DrainItem(token))
            waiter = asyncio.ensure_future(event.wait())
            await asyncio.wait(
                {waiter, *self._tasks}, timeout=DRAIN_LIMIT_S, return_when=asyncio.FIRST_COMPLETED
            )
            waiter.cancel()
            await self._await_acks()
        await self._side.flush()
        return self.outcome()

    async def _await_acks(self) -> None:
        deadline = self._deps.setup.ack_grace_ms / MS_PER_SECOND
        loop = asyncio.get_running_loop()
        until = loop.time() + deadline
        while any(t.playback_completed and not t.browser_completed for t in self.tracks):
            remaining = until - loop.time()
            if remaining <= 0 or self._cancelled:
                return
            self._acks.clear()
            with suppress(TimeoutError):
                async with asyncio.timeout(remaining):
                    await self._acks.wait()

    def outcome(self) -> SpeechOutcome:
        return SpeechOutcome(
            tracks=tuple(self.tracks), first_audio_ms=self._first_audio_ms, failure=self._failure
        )

    # ------------------------------------------------------------- cancel --
    async def cancel(self) -> None:
        """Stop speech after the fence advanced: TTS, app queue, AudioSource, browser."""
        if self._cancelled:
            return
        self._cancelled = True
        await self._deps.tts.cancel_turn(self._turn_id)
        while not self._frames.empty():
            item = self._frames.get_nowait()
            if isinstance(item, DrainItem):
                self._release(item)
            elif isinstance(item, FrameItem):
                item.track.stale_frames += 1
        await self._deps.transport.clear_playback()
        self.cleared_at_ms = self._deps.clock.monotonic_ms()
        playing, self._playing = self._playing, None
        if playing is not None and not playing.playback_completed:
            await self._deps.publisher.publish_playback(
                "cancelled", playing.identity, EventType.PLAYBACK_CANCELLED, turn_id=self._turn_id
            )
            self._record(EventType.PLAYBACK_CANCELLED, playing)
        self._acks.set()

    def on_ack(self, ack: PlaybackAck) -> bool:
        """Record a browser acknowledgement for one of this turn's pieces (evidence only)."""
        track = next((t for t in self.tracks if t.identity == ack.identity), None)
        if track is None:
            return False
        if ack.ack is PlaybackAckKind.STARTED:
            track.browser_started = True
        elif ack.ack is PlaybackAckKind.COMPLETED:
            track.browser_completed = True
        elif ack.ack is PlaybackAckKind.FAILED:
            track.browser_failed = True
        self._acks.set()
        return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task
        for event in self._drains.values():
            event.set()
        await self._side.close()
        await self._writer.close()
