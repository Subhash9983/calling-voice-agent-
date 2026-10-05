"""Agent-configuration and session contracts (docs/04 §5-§9)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from voice_agent.contracts.base import CanonicalId
from voice_agent.contracts.enums import (
    AgentActivityState,
    CalculationStatus,
    DisconnectReason,
    SessionStatus,
)
from voice_agent.control_api.schemas.common import ApiModel, Cursor, IsoUtcTimestamp

DEFAULT_SESSION_PAGE = 25
MAX_SESSION_PAGE = 100


def _omit_none(value: object) -> bool:
    return value is None


# ----------------------------------------------------------------- requests --
class AgentConfigListParams(ApiModel):
    status: Literal["active"] = "active"
    environment: Literal["development", "rd"] | None = None


class SessionCreateRequest(ApiModel):
    """Only these fields; provider/model/prompt/voice/endpoint are never accepted."""

    client_request_id: CanonicalId
    agent_config_id: CanonicalId
    channel: Literal["browser"]
    session_mode: Literal["interactive_test"]
    language_mode: Literal["auto"]


class JoinTokenRequest(ApiModel):
    client_request_id: CanonicalId


class EndSessionRequest(ApiModel):
    client_request_id: CanonicalId
    reason: DisconnectReason


class SessionListParams(ApiModel):
    status: SessionStatus | None = None
    agent_config_id: CanonicalId | None = None
    created_before: IsoUtcTimestamp | None = None
    cursor: Cursor | None = None
    limit: Annotated[int, Field(ge=1, le=MAX_SESSION_PAGE)] = DEFAULT_SESSION_PAGE


# ---------------------------------------------------------------- responses --
class AgentConfigFeatures(ApiModel):
    partial_transcripts: bool
    interruptions: bool


class AgentConfigView(ApiModel):
    agent_config_id: str
    agent_id: str
    name: str
    description: str | None
    version: int
    transport: str
    stt: str
    conversation_engine: str
    tts: str
    language_mode: str
    features: AgentConfigFeatures


class ConfigurationLabels(ApiModel):
    agent_config_id: str
    name: str | None
    version: int
    transport: str
    stt: str
    conversation_engine: str
    tts: str


class SessionBrief(ApiModel):
    session_id: str
    status: SessionStatus
    agent_activity_state: AgentActivityState | None
    created_at: datetime
    maximum_session_ms: int


class TransportJoin(ApiModel):
    """Join data; the token fields are omitted when no token is issued."""

    provider: str
    url: str
    room_name: str | None = Field(default=None, exclude_if=_omit_none)
    participant_identity: str | None = Field(default=None, exclude_if=_omit_none)
    join_token: str | None = Field(default=None, exclude_if=_omit_none, repr=False)
    token_expires_at: datetime | None = Field(default=None, exclude_if=_omit_none)


class SessionCreateResponse(ApiModel):
    idempotent_replay: bool
    session: SessionBrief
    transport: TransportJoin
    configuration: ConfigurationLabels
    request_id: str


class JoinTokenData(ApiModel):
    session_id: str
    transport: TransportJoin


class EndSessionData(ApiModel):
    session_id: str
    status: SessionStatus
    revision: int
    termination_request_revision: int | None
    disconnect_reason: DisconnectReason | None


class RecordingView(ApiModel):
    mode: Literal["off"]
    status: Literal["not_requested"]


class LanguageSummaryView(ApiModel):
    language_mode: str


class CostSummaryView(ApiModel):
    """Derived from the latest session-scope cost run (any status; WP11).

    ``estimated_total_usd`` is ``None`` while nothing is priced; a ``partial``
    status means some attempt's usage or rate is unavailable (never zero).
    """

    currency: str
    rate_card_version: str | None
    calculation_status: CalculationStatus
    calculation_run_id: str | None = None
    estimated_total_usd: str | None = None
    reconciled: bool | None = None


class SessionView(ApiModel):
    """Browser-safe session summary; transcripts and provider diagnostics excluded.

    ``turn_summary``/``error_summary``/``latency_summary`` are derived from the
    stored child records (docs/02 §6); a metric without samples is omitted
    and unavailable values are never zero.
    """

    session_id: str
    status: SessionStatus
    agent_activity_state: AgentActivityState | None
    revision: int
    configuration: ConfigurationLabels
    created_at: datetime
    updated_at: datetime
    connecting_at: datetime | None
    ending_at: datetime | None
    ended_at: datetime | None
    maximum_session_ms: int
    language_summary: LanguageSummaryView
    recording: RecordingView
    termination_requested: bool
    disconnect_reason: DisconnectReason | None
    turn_summary: dict[str, int] | None = None
    error_summary: dict[str, int] | None = None
    latency_summary: dict[str, dict[str, int | float]] | None = None
    cost_summary: CostSummaryView


class SessionListItem(ApiModel):
    session_id: str
    status: SessionStatus
    agent_config_id: str
    agent_config_version: int
    created_at: datetime
    ended_at: datetime | None
    disconnect_reason: DisconnectReason | None
