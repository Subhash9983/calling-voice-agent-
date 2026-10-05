"""Build the STT check activity for one admitted job (docs/14 §13; docs/12 §9-§10).

Everything comes from the immutable, server-approved agent configuration:
the Deepgram section (model, options, empty keyterm list), the turn-handling
policy (VAD thresholds, 550 ms silence, 700 ms endpoint), the retry policy,
and the rate-card version. The Deepgram key is resolved late through the WP3
``CredentialResolver`` and handed to the SDK binding as a ``SecretStr``; the
prewarmed Silero model is shared from the worker process.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from pydantic import SecretStr

from voice_agent.agent_worker.admission import JobAdmission
from voice_agent.agent_worker.realtime_publisher import RealtimePublisher
from voice_agent.agent_worker.session_runner import ActivityContext, ActivityFactory
from voice_agent.agent_worker.stt_check import GenerationGate, SttCheck, SttCheckSetup
from voice_agent.agent_worker.stt_evidence import CostRunWriter, EvidenceContext, SttEvidence
from voice_agent.contracts.cost import RateCard
from voice_agent.contracts.enums import OperationComponent
from voice_agent.contracts.policies import RetryPolicy
from voice_agent.contracts.stt import SttStreamConfig
from voice_agent.costing.rate_card import (
    PHASE0_PROMO_RATE_CARD_ID,
    PHASE0_RATE_CARD_ID,
    phase0_promotional_rate_card,
    phase0_rate_card,
)
from voice_agent.domain.agent_config import AgentConfig, SttSection
from voice_agent.orchestration.retry import backoff_ms
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.documents.timeline import WriteContext
from voice_agent.persistence.mongodb.repositories.cost_entries import MongoCostEntryStore
from voice_agent.persistence.mongodb.repositories.timeline import (
    MongoOperationRepository,
    MongoTurnRepository,
)
from voice_agent.ports.clock import Clock, IdGenerator
from voice_agent.ports.control_plane import SessionEventLog
from voice_agent.ports.speech_activity import SpeechActivityPort
from voice_agent.ports.stt import STTPort
from voice_agent.security.credentials import CredentialResolver
from voice_agent.security.settings import BootstrapSettings
from voice_agent.speech_activity.silero import SileroModelHandle, SileroSpeechActivityDetector
from voice_agent.stt_adapters.deepgram.adapter import DeepgramSttAdapter
from voice_agent.stt_adapters.deepgram.connection import DeepgramConnector
from voice_agent.stt_adapters.deepgram.options import DEEPGRAM_PROVIDER
from voice_agent.stt_adapters.deepgram.sdk_binding import SdkDeepgramConnector

_RATE_CARDS: Final[Mapping[str, Callable[[], RateCard]]] = {
    PHASE0_RATE_CARD_ID: phase0_rate_card,
    PHASE0_PROMO_RATE_CARD_ID: phase0_promotional_rate_card,
}
_LOGGER = logging.getLogger("voice_agent.agent_worker.stt")


def uses_real_stt(config: AgentConfig) -> bool:
    return config.stt.provider == DEEPGRAM_PROVIDER


def stt_stream_config(section: SttSection) -> SttStreamConfig:
    """Normalized STT configuration from the approved section (validated again here)."""
    options = section.safe_options
    return SttStreamConfig.model_validate(
        {
            "language_mode": section.language_mode,
            "expected_languages": tuple(options.get("expected_languages") or ()),  # type: ignore[arg-type]
            "code_switching": options.get("code_switching"),
            "partial_transcripts": section.partial_transcripts,
            "punctuation": options.get("punctuation"),
            "smart_formatting": options.get("smart_formatting"),
            "sample_rate_hz": section.sample_rate_hz,
            "keyterms": tuple(options.get("keyterms") or ()),  # type: ignore[arg-type]
        }
    )


def rate_card_for(config: AgentConfig) -> RateCard | None:
    factory = _RATE_CARDS.get(config.cost_rate_card_version)
    return None if factory is None else factory()


def jittered_backoff(policy: RetryPolicy) -> Callable[[int], int]:
    def delay(failed_attempt: int) -> int:
        return backoff_ms(policy, failed_attempt, random.random())  # noqa: S311 - jitter only

    return delay


def sdk_connector(api_key: SecretStr) -> DeepgramConnector:
    return SdkDeepgramConnector(api_key)


@dataclass(frozen=True, slots=True)
class SttSessionDeps:
    settings: BootstrapSettings
    silero: SileroModelHandle
    persistence: MongoPersistence
    events: SessionEventLog
    clock: Clock
    ids: IdGenerator
    connector_factory: Callable[[SecretStr], DeepgramConnector] = sdk_connector


def _evidence(deps: SttSessionDeps, admission: JobAdmission, generation: int) -> SttEvidence:
    record, config = admission.record, admission.config
    write = WriteContext(
        session_id=record.session_id,
        correlation_id=record.correlation_id,
        agent_config_id=record.agent_config_id,
        environment=record.environment,
        adapter_versions={
            OperationComponent.STT: config.stt.adapter_version,
            OperationComponent.CONVERSATION_ENGINE: config.conversation_engine.adapter_version,
            OperationComponent.TTS: config.tts.adapter_version,
        },
    )
    card = rate_card_for(config)
    costs: CostRunWriter | None = None if card is None else MongoCostEntryStore(deps.persistence)
    if card is None:
        _LOGGER.warning("stt.rate_card_unknown")
    return SttEvidence(
        EvidenceContext(
            session_id=record.session_id,
            correlation_id=record.correlation_id,
            agent_config_id=record.agent_config_id,
            environment=record.environment,
            worker_generation=generation,
        ),
        turns=MongoTurnRepository(deps.persistence, context=write, clock=deps.clock),
        operations=MongoOperationRepository(deps.persistence, context=write, clock=deps.clock),
        events=deps.events,
        costs=costs,
        rate_card=card or phase0_rate_card(),
        clock=deps.clock,
        ids=deps.ids,
    )


GateFactory = Callable[[SttEvidence, RealtimePublisher], GenerationGate]


@dataclass(frozen=True, slots=True)
class SttParts:
    """The STT pipeline pieces shared by the STT check and the WP10 orchestrator."""

    setup: SttCheckSetup
    stt: STTPort
    detector: SpeechActivityPort
    evidence: SttEvidence
    publisher: RealtimePublisher


def stt_parts(context: ActivityContext, admission: JobAdmission, deps: SttSessionDeps) -> SttParts:
    config = admission.config
    credential_ref = config.stt.credential_ref
    if credential_ref is None:  # pragma: no cover - rejected by the approved profile
        raise RuntimeError("the STT section has no credential reference")
    api_key = CredentialResolver(deps.settings).resolve(credential_ref)
    connector = deps.connector_factory(api_key)
    policy = config.turn_handling_policy()
    stt = DeepgramSttAdapter(
        connector,
        session_id=context.session_id,
        worker_generation=context.worker_generation,
        clock=deps.clock,
        ids=deps.ids,
        model=config.stt.model,
        retry=config.retry_policy,
        retry_delay_ms=jittered_backoff(config.retry_policy),
    )
    publisher = RealtimePublisher(
        context.transport,
        session_id=context.session_id,
        correlation_id=context.correlation_id,
        clock=deps.clock,
        ids=deps.ids,
    )
    return SttParts(
        setup=SttCheckSetup(
            session_id=context.session_id,
            worker_generation=context.worker_generation,
            policy=policy,
            stt_config=stt_stream_config(config.stt),
            prefix_padding_ms=config.turn_handling.vad.prefix_padding_ms,
        ),
        stt=stt,
        detector=SileroSpeechActivityDetector(deps.silero.new_model(), policy),
        evidence=_evidence(deps, admission, context.worker_generation),
        publisher=publisher,
    )


def build_stt_check(
    context: ActivityContext,
    admission: JobAdmission,
    deps: SttSessionDeps,
    *,
    gate_factory: GateFactory | None = None,
) -> SttCheck:
    """The STT check; ``gate_factory`` adds a generation gate sharing its evidence/publisher."""
    parts = stt_parts(context, admission, deps)
    return SttCheck(
        context.transport,
        parts.setup,
        stt=parts.stt,
        detector=parts.detector,
        evidence=parts.evidence,
        publisher=parts.publisher,
        clock=deps.clock,
        ids=deps.ids,
        gate=None if gate_factory is None else gate_factory(parts.evidence, parts.publisher),
    )


def stt_activity(admission: JobAdmission, deps: SttSessionDeps) -> ActivityFactory:
    async def run(context: ActivityContext) -> None:
        await build_stt_check(context, admission, deps).run()

    return run
