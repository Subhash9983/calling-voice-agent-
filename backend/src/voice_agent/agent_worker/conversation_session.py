"""Build the real conversation activity for one admitted job (WP10; docs/14 §16).

The same approved providers and configuration as the WP9 TTS check
(Deepgram Nova-3 + GPT-6 Luna + Sarvam Bulbul v3 ``priya`` over the LiveKit
session), but run by the authoritative :class:`ConversationOrchestrator`:
barge-in during playback, the deterministic greeting, clarification, and
the approved session timeouts. A worker-crash recovery claim (worker
generation > 1) never replays the greeting.
"""

from __future__ import annotations

from voice_agent.agent_worker.admission import JobAdmission
from voice_agent.agent_worker.conversation_gate import OrchestratedGate
from voice_agent.agent_worker.conversation_orchestrator import ConversationOrchestrator
from voice_agent.agent_worker.conversation_policy import ConversationTimeouts
from voice_agent.agent_worker.llm_session import conversation_engine, conversation_setup
from voice_agent.agent_worker.session_runner import ActivityContext, ActivityFactory
from voice_agent.agent_worker.stt_session import stt_parts
from voice_agent.agent_worker.tts_gate import SpeechConfig
from voice_agent.agent_worker.tts_session import (
    TtsSessionDeps,
    speech_setup,
    tts_adapter,
    voice_config,
)
from voice_agent.domain.worker_lease import INITIAL_GENERATION


def build_conversation(
    context: ActivityContext, admission: JobAdmission, deps: TtsSessionDeps
) -> ConversationOrchestrator:
    config = admission.config
    stt_deps = deps.llm.stt
    parts = stt_parts(context, admission, stt_deps)
    gate = OrchestratedGate(
        conversation_setup(context, admission),
        engine=conversation_engine(admission, deps.llm),
        speech=SpeechConfig(
            tts=tts_adapter(admission, deps),
            transport=context.transport,
            voice=voice_config(config.tts),
            setup=speech_setup(context, config),
        ),
        evidence=parts.evidence,
        publisher=parts.publisher,
        clock=stt_deps.clock,
        ids=stt_deps.ids,
    )
    orchestrator = ConversationOrchestrator(
        context.transport,
        parts.setup,
        stt=parts.stt,
        detector=parts.detector,
        evidence=parts.evidence,
        publisher=parts.publisher,
        clock=stt_deps.clock,
        ids=stt_deps.ids,
        gate=gate,
        timeouts=ConversationTimeouts.from_policy(
            config.timeout_policy, deadline_at=admission.record.maximum_duration_deadline_at
        ),
        request_end=context.request_end,
        greeting_enabled=context.worker_generation == INITIAL_GENERATION,
    )
    context.add_lifecycle_listener(orchestrator.on_transport_event)
    return orchestrator


def conversation_activity(admission: JobAdmission, deps: TtsSessionDeps) -> ActivityFactory:
    async def run(context: ActivityContext) -> None:
        await build_conversation(context, admission, deps).run()

    return run
