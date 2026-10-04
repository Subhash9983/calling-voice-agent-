"""Build the LLM check activity for one admitted job (docs/14 §14 WP8; docs/12 §9-§10).

The STT check (local VAD + Turn Manager + Deepgram) plus a
:class:`ConversationGate` running the OpenAI GPT-6 Luna adapter. Everything
comes from the immutable, server-approved agent configuration: the exact
``phase0_general_voice_assistant_v1`` instruction (re-verified here against
the canonical checksum), the 250-token cap, the LLM timeouts, and the retry
policy. The OpenAI key is resolved late through the WP3 ``CredentialResolver``
and handed to the SDK binding as a ``SecretStr``; it is never logged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from pydantic import SecretStr

from voice_agent.agent_worker.admission import JobAdmission
from voice_agent.agent_worker.llm_gate import ConversationGate, ConversationSetup
from voice_agent.agent_worker.realtime_publisher import RealtimePublisher
from voice_agent.agent_worker.session_runner import ActivityContext, ActivityFactory
from voice_agent.agent_worker.stt_check import SttCheck
from voice_agent.agent_worker.stt_evidence import SttEvidence
from voice_agent.agent_worker.stt_session import SttSessionDeps, build_stt_check, uses_real_stt
from voice_agent.conversation_adapters.openai.adapter import (
    OpenAiConversationAdapter,
    OpenAiTimeouts,
)
from voice_agent.conversation_adapters.openai.connection import ResponsesConnector
from voice_agent.conversation_adapters.openai.options import OPENAI_PROVIDER
from voice_agent.conversation_adapters.openai.sdk_binding import SdkResponsesConnector
from voice_agent.domain.agent_config import AgentConfig
from voice_agent.provider_registry.phase0_prompt import is_canonical_phase0_prompt
from voice_agent.security.credentials import CredentialResolver

MS_PER_SECOND: Final = 1000
# The HTTP client deadline sits just above the adapter's own total deadline.
HTTP_TIMEOUT_SLACK_S: Final = 5.0


class PromptNotApprovedError(RuntimeError):
    """The configuration's prompt is not the exact approved Phase 0 instruction."""


def uses_real_llm(config: AgentConfig) -> bool:
    return uses_real_stt(config) and config.conversation_engine.provider == OPENAI_PROVIDER


def sdk_connector(api_key: SecretStr, timeout_s: float) -> ResponsesConnector:
    return SdkResponsesConnector(api_key, timeout_s=timeout_s)


@dataclass(frozen=True, slots=True)
class LlmSessionDeps:
    stt: SttSessionDeps
    connector_factory: Callable[[SecretStr, float], ResponsesConnector] = sdk_connector


def conversation_setup(context: ActivityContext, admission: JobAdmission) -> ConversationSetup:
    config = admission.config
    section = config.conversation_engine
    if not is_canonical_phase0_prompt(
        section.prompt_id, section.system_instruction_version, section.prompt_checksum
    ):
        raise PromptNotApprovedError("conversation prompt is not the approved Phase 0 text")
    return ConversationSetup(
        session_id=context.session_id,
        correlation_id=context.correlation_id,
        worker_generation=context.worker_generation,
        agent_config_id=config.agent_config_id,
        config_checksum=config.config_checksum,
        prompt_id=section.prompt_id,
        prompt_version=int(section.system_instruction_version),
        prompt_checksum=section.prompt_checksum,
        system_instruction=section.system_instruction,
        max_output_tokens=section.max_output_tokens,
        provider=section.provider,
        model=section.model,
        retry=config.retry_policy,
    )


def _engine(admission: JobAdmission, deps: LlmSessionDeps) -> OpenAiConversationAdapter:
    config = admission.config
    credential_ref = config.conversation_engine.credential_ref
    if credential_ref is None:  # pragma: no cover - rejected by the approved profile
        raise RuntimeError("the conversation section has no credential reference")
    api_key = CredentialResolver(deps.stt.settings).resolve(credential_ref)
    timeouts = OpenAiTimeouts(
        first_token_ms=config.timeout_policy.llm_first_token_ms,
        total_ms=config.timeout_policy.llm_total_ms,
    )
    http_timeout_s = timeouts.total_ms / MS_PER_SECOND + HTTP_TIMEOUT_SLACK_S
    return OpenAiConversationAdapter(
        deps.connector_factory(api_key, http_timeout_s),
        clock=deps.stt.clock,
        model=config.conversation_engine.model,
        timeouts=timeouts,
    )


def build_llm_check(
    context: ActivityContext, admission: JobAdmission, deps: LlmSessionDeps
) -> SttCheck:
    setup = conversation_setup(context, admission)
    engine = _engine(admission, deps)

    def gate(evidence: SttEvidence, publisher: RealtimePublisher) -> ConversationGate:
        return ConversationGate(
            setup,
            engine=engine,
            evidence=evidence,
            publisher=publisher,
            clock=deps.stt.clock,
            ids=deps.stt.ids,
        )

    return build_stt_check(context, admission, deps.stt, gate_factory=gate)


def llm_activity(admission: JobAdmission, deps: LlmSessionDeps) -> ActivityFactory:
    async def run(context: ActivityContext) -> None:
        await build_llm_check(context, admission, deps).run()

    return run
