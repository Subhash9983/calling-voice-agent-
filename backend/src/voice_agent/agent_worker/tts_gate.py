"""TTS-check generation gate: accepted transcript -> GPT-6 Luna -> Bulbul v3 speech (WP9).

Extends the WP8 :class:`ConversationGate` (one fenced generation per accepted
turn, retries, fallback, history) so authorized segments are *spoken*
instead of only published as text:

- each delivered (authorized) segment goes to the turn's
  :class:`SpeechTurn`; text reaches ``va.response.v1`` only when its audio
  starts playing, so the browser shows what is being heard;
- the first played frame moves the turn to ``audio_streaming`` and the
  agent state to ``speaking``;
- a newer accepted turn (or :meth:`interrupt`) follows the canonical order:
  the gate's fence advances, the LLM stream is cancelled, then speech is
  cancelled (TTS, application queue, ``AudioSource.clear_queue()``) and the
  browser is told ``cancelled``/``interrupted`` (docs/09 §13);
- turn evidence keeps four stages apart: ``generated_text`` (LLM),
  normalized pieces (operation summaries), ``synthesized_text`` (pieces the
  provider acknowledged), and ``spoken_text`` + accuracy (pieces that reached
  playback); history and the final browser text use only the original text
  of segments that were actually heard;
- a TTS failure fails the turn (partial speech is never repaired or
  replayed); with nothing heard the user gets a safe error message.
"""

from __future__ import annotations

import dataclasses
import random
from collections.abc import Awaitable, Callable
from contextlib import suppress

from voice_agent.agent_worker.llm_gate import ConversationGate, ConversationSetup, _TurnRun
from voice_agent.agent_worker.realtime_publisher import RealtimePublisher
from voice_agent.agent_worker.speech_output import SpeechDeps, SpeechTurn
from voice_agent.agent_worker.speech_synthesis import SpeechSetup
from voice_agent.agent_worker.speech_tracks import SpeechOutcome
from voice_agent.agent_worker.stt_evidence import SttEvidence
from voice_agent.contracts.enums import AgentActivityState
from voice_agent.contracts.transport import PlaybackAck
from voice_agent.contracts.tts import TtsVoiceConfig
from voice_agent.domain.turn import ConversationTurn
from voice_agent.orchestration.response_generation import DeliveredSegment, GenerationResult
from voice_agent.ports.clock import Clock, IdGenerator
from voice_agent.ports.conversation import ConversationEnginePort
from voice_agent.ports.transport import SessionTransportPort
from voice_agent.ports.tts import TTSPort

_EMPTY = SpeechOutcome(tracks=(), first_audio_ms=None, failure=None)


@dataclasses.dataclass(frozen=True, slots=True)
class SpeechConfig:
    tts: TTSPort
    transport: SessionTransportPort
    voice: TtsVoiceConfig
    setup: SpeechSetup


class SpeakingConversationGate(ConversationGate):
    def __init__(
        self,
        setup: ConversationSetup,
        *,
        engine: ConversationEnginePort,
        speech: SpeechConfig,
        evidence: SttEvidence,
        publisher: RealtimePublisher,
        clock: Clock,
        ids: IdGenerator,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        super().__init__(
            setup,
            engine=engine,
            evidence=evidence,
            publisher=publisher,
            clock=clock,
            ids=ids,
            jitter=jitter,
        )
        self._speech_config = speech
        self._speech_deps = SpeechDeps(
            setup=speech.setup,
            tts=speech.tts,
            transport=speech.transport,
            fence=self._fence,
            evidence=evidence,
            publisher=publisher,
            clock=clock,
            ids=ids,
            jitter=jitter,
        )
        self._speech: dict[str, SpeechTurn] = {}
        self._outcomes: dict[str, SpeechOutcome] = {}
        self._tts_open = False
        self.ignored_acks = 0

    # ---------------------------------------------------------- lifecycle --
    async def start(self) -> None:
        """Open the TTS session (and prewarm its connection) before the first turn."""
        if not self._tts_open:
            await self._speech_config.tts.open_session(self._speech_config.voice)
            self._tts_open = True

    async def interrupt(self) -> None:
        """Accepted barge-in: cancel the active response and its speech (docs/09 §13)."""
        await self._supersede_active()

    async def on_playback_ack(self, ack: PlaybackAck) -> None:
        for speech in self._speech.values():
            if speech.on_ack(ack):
                return
        self.ignored_acks += 1

    # ------------------------------------------------------------ speech --
    def _speech_for(self, run: _TurnRun) -> SpeechTurn:
        turn_id = run.turn.turn_id
        speech = self._speech.get(turn_id)
        if speech is None:
            speech = SpeechTurn(
                self._speech_deps,
                turn_stamp=self._fence.stamp(turn_id=turn_id),
                on_first_audio=self._first_audio_callback(run),
                on_audible=self._audible_callback(run),
            )
            self._speech[turn_id] = speech
        return speech

    def _first_audio_callback(self, run: _TurnRun) -> Callable[[], Awaitable[None]]:
        async def first_audio() -> None:
            if run.turn.is_terminal:
                return
            run.turn = run.turn.authorize_audio()
            await self._evidence.save_turn(run.turn)
            await self._publisher.publish_state(AgentActivityState.SPEAKING)

        return first_audio

    def _audible_callback(self, run: _TurnRun) -> Callable[[DeliveredSegment], Awaitable[None]]:
        heard: list[DeliveredSegment] = []

        async def audible(segment: DeliveredSegment) -> None:
            if segment.is_fallback:
                return  # the fallback phrase reaches the browser in the final message only
            heard.append(segment)
            await self._publisher.publish_response_segment(
                " ".join(s.text for s in heard),
                turn_id=run.turn.turn_id,
                segment_sequence=segment.sequence,
            )

        return audible

    async def _on_delivered(self, run: _TurnRun, segment: DeliveredSegment) -> None:
        await self.start()
        await self._speech_for(run).enqueue(segment)

    def _fallback_sink(self, run: _TurnRun) -> Callable[[DeliveredSegment], Awaitable[None]]:
        async def sink(segment: DeliveredSegment) -> None:
            run.delivered.append(segment)
            await self._on_delivered(run, segment)

        return sink

    async def _on_superseded(self, run: _TurnRun) -> None:
        speech = self._speech.get(run.turn.turn_id)
        if speech is None:
            return
        await speech.cancel()
        if any(track.playback_started for track in speech.tracks):
            await self._publisher.publish_state(AgentActivityState.INTERRUPTED, force=True)

    # ------------------------------------------------------------ settle --
    async def _drain(self, run: _TurnRun) -> SpeechOutcome:
        speech = self._speech.get(run.turn.turn_id)
        outcome = _EMPTY if speech is None else await speech.drain()
        self._outcomes[run.turn.turn_id] = outcome
        return outcome

    async def _settle_output(self, run: _TurnRun, result: GenerationResult) -> GenerationResult:
        outcome = await self._drain(run)
        speech = self._speech.get(run.turn.turn_id)
        cancelled = result.cancelled or (speech is not None and speech.cancelled)
        failure = result.failure
        if outcome.failure is not None and not cancelled:
            failure = failure or outcome.failure
            if not outcome.spoken_tracks:
                await self._publisher.publish_error(
                    "speech_unavailable",
                    "The spoken response could not be played.",
                    retryable=True,
                    turn_id=run.turn.turn_id,
                )
        spoken = tuple(s for s in outcome.spoken_segments if not s.is_fallback)
        return dataclasses.replace(result, delivered=spoken, failure=failure, cancelled=cancelled)

    async def _settle_fallback(
        self, run: _TurnRun, spoken: tuple[DeliveredSegment, ...]
    ) -> tuple[DeliveredSegment, ...]:
        if not spoken:
            return spoken
        outcome = await self._drain(run)
        return tuple(s for s in outcome.spoken_segments if s.is_fallback)

    def _outcome(self, run: _TurnRun) -> SpeechOutcome:
        return self._outcomes.get(run.turn.turn_id, _EMPTY)

    def _record_output(self, run: _TurnRun, turn: ConversationTurn) -> ConversationTurn:
        outcome = self._outcome(run)
        if outcome.synthesized_text:
            turn = turn.record_synthesized(outcome.synthesized_text)
        if outcome.spoken_text:
            turn = turn.record_spoken(outcome.spoken_text, outcome.accuracy)
        return turn

    def _final_text(self, run: _TurnRun) -> str:
        return " ".join(segment.text for segment in self._outcome(run).spoken_segments)

    async def _finish(self, run: _TurnRun, result: GenerationResult) -> None:
        try:
            await super()._finish(run, result)
        finally:
            await self._release(run)

    async def _crashed(self, run: _TurnRun) -> None:
        speech = self._speech.get(run.turn.turn_id)
        if speech is not None:
            with suppress(Exception):
                await speech.cancel()
        await super()._crashed(run)
        await self._release(run)

    async def _release(self, run: _TurnRun) -> None:
        self._outcomes.pop(run.turn.turn_id, None)
        speech = self._speech.pop(run.turn.turn_id, None)
        if speech is not None:
            await speech.close()

    async def _close_output(self) -> None:
        for speech in list(self._speech.values()):
            with suppress(Exception):
                await speech.cancel()
            await speech.close()
        self._speech.clear()
        await self._speech_config.tts.close()
