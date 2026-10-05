"""Build the TTS check activity for one admitted job (docs/14 §15 WP9; docs/12 §9-§10).

The LLM check (local VAD + Turn Manager + Deepgram + GPT-6 Luna) whose
generation gate speaks every authorized segment through Sarvam Bulbul v3
``priya`` and the session's LiveKit ``agent-audio`` source. Everything comes
from the immutable, server-approved configuration: the TTS section (model,
voice, pace, 24 kHz linear16), ``tts_first_audio_ms`` (5 s), and the retry
policy. The Sarvam key is resolved late through the WP3
``CredentialResolver`` and handed to the SDK binding as a ``SecretStr``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from pydantic import SecretStr

from voice_agent.agent_worker.admission import JobAdmission
from voice_agent.agent_worker.llm_session import (
    LlmSessionDeps,
    conversation_engine,
    conversation_setup,
    uses_real_llm,
)
from voice_agent.agent_worker.realtime_publisher import RealtimePublisher
from voice_agent.agent_worker.session_runner import ActivityContext, ActivityFactory
from voice_agent.agent_worker.speech_synthesis import SpeechSetup
from voice_agent.agent_worker.stt_check import SttCheck
from voice_agent.agent_worker.stt_evidence import SttEvidence
from voice_agent.agent_worker.stt_session import build_stt_check
from voice_agent.agent_worker.tts_gate import SpeakingConversationGate, SpeechConfig
from voice_agent.contracts.tts import TtsVoiceConfig
from voice_agent.domain.agent_config import AgentConfig, TtsSection
from voice_agent.security.credentials import CredentialResolver
from voice_agent.tts_adapters.sarvam.adapter import SarvamTimeouts, SarvamTtsAdapter
from voice_agent.tts_adapters.sarvam.connection import SarvamConnector
from voice_agent.tts_adapters.sarvam.options import SARVAM_PROVIDER
from voice_agent.tts_adapters.sarvam.sdk_binding import SdkSarvamConnector


def uses_real_tts(config: AgentConfig) -> bool:
    return uses_real_llm(config) and config.tts.provider == SARVAM_PROVIDER


def sdk_tts_connector(api_key: SecretStr) -> SarvamConnector:
    return SdkSarvamConnector(api_key)


@dataclass(frozen=True, slots=True)
class TtsSessionDeps:
    llm: LlmSessionDeps
    connector_factory: Callable[[SecretStr], SarvamConnector] = sdk_tts_connector


def voice_config(section: TtsSection) -> TtsVoiceConfig:
    rate = section.speaking_rate if section.speaking_rate is not None else 1.0
    return TtsVoiceConfig(
        provider=section.provider,
        model=section.model,
        voice_id=section.voice_id,
        speaking_rate=Decimal(str(rate)),
    )


def speech_setup(context: ActivityContext, config: AgentConfig) -> SpeechSetup:
    section = config.tts
    return SpeechSetup(
        session_id=context.session_id,
        worker_generation=context.worker_generation,
        provider=section.provider,
        model=section.model,
        voice_id=section.voice_id,
        retry=config.retry_policy,
    )


def tts_adapter(admission: JobAdmission, deps: TtsSessionDeps) -> SarvamTtsAdapter:
    config = admission.config
    credential_ref = config.tts.credential_ref
    if credential_ref is None:  # pragma: no cover - rejected by the approved profile
        raise RuntimeError("the TTS section has no credential reference")
    api_key = CredentialResolver(deps.llm.stt.settings).resolve(credential_ref)
    timeouts = SarvamTimeouts(first_audio_ms=config.timeout_policy.tts_first_audio_ms)
    return SarvamTtsAdapter(
        deps.connector_factory(api_key), clock=deps.llm.stt.clock, timeouts=timeouts
    )


def build_tts_check(
    context: ActivityContext, admission: JobAdmission, deps: TtsSessionDeps
) -> SttCheck:
    config = admission.config
    setup = conversation_setup(context, admission)
    engine = conversation_engine(admission, deps.llm)
    speech = SpeechConfig(
        tts=tts_adapter(admission, deps),
        transport=context.transport,
        voice=voice_config(config.tts),
        setup=speech_setup(context, config),
    )
    stt = deps.llm.stt

    def gate(evidence: SttEvidence, publisher: RealtimePublisher) -> SpeakingConversationGate:
        return SpeakingConversationGate(
            setup,
            engine=engine,
            speech=speech,
            evidence=evidence,
            publisher=publisher,
            clock=stt.clock,
            ids=stt.ids,
        )

    return build_stt_check(context, admission, stt, gate_factory=gate)


def tts_activity(admission: JobAdmission, deps: TtsSessionDeps) -> ActivityFactory:
    async def run(context: ActivityContext) -> None:
        await build_tts_check(context, admission, deps).run()

    return run
