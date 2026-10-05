"""LiveKit job admission: validate the locator against durable state (docs/05 §3; docs/06 §4, §7).

Job metadata is only a locator. Admission reloads the durable session and
the exact immutable configuration and accepts an initial job only when all
of these hold; otherwise it rejects with a safe reason code and no paid
provider can start:

- the locator parses, and matches the session's environment, configuration
  and correlation ID;
- the session is ``connecting`` with no termination request, no live worker
  lease, and time left before its maximum duration; or, for a worker-crash
  replacement (WP10, docs/05 §3, §21), ``active`` with a stored recovery
  authorization whose ``recovery_dispatch_id`` matches the job's locator and
  whose recovery deadline has not passed;
- the job's room is the session's backend-owned room and the session has an
  opaque agent identity (used verbatim as the participant identity at
  ``req.accept``);
- the configuration version/checksum is the one the session references,
  uses the LiveKit transport, and recording stays off.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol

from voice_agent.contracts.dispatch import (
    DispatchLocator,
    DispatchMetadataError,
    decode_dispatch_metadata,
)
from voice_agent.contracts.enums import SessionStatus
from voice_agent.domain.agent_config import AgentConfig
from voice_agent.domain.control_session import SessionRecord

LIVEKIT_TRANSPORT: Final = "livekit"


class RejectReason(StrEnum):
    INVALID_METADATA = "invalid_metadata"
    ENVIRONMENT_MISMATCH = "environment_mismatch"
    SESSION_NOT_FOUND = "session_not_found"
    LOCATOR_MISMATCH = "locator_mismatch"
    NOT_CLAIMABLE = "not_claimable"
    TERMINATION_REQUESTED = "termination_requested"
    WORKER_ALREADY_ASSIGNED = "worker_already_assigned"
    MAXIMUM_DURATION_REACHED = "maximum_duration_reached"
    ROOM_MISMATCH = "room_mismatch"
    CONFIGURATION_UNUSABLE = "configuration_unusable"


class AdmissionRejectedError(RuntimeError):
    def __init__(self, reason: RejectReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class SessionReader(Protocol):
    async def get(self, session_id: str) -> SessionRecord | None: ...


class ConfigReader(Protocol):
    async def get(self, agent_config_id: str) -> AgentConfig | None: ...


@dataclass(frozen=True, slots=True)
class JobAdmission:
    locator: DispatchLocator
    record: SessionRecord
    config: AgentConfig
    agent_identity: str
    browser_identity: str
    room_name: str

    @property
    def is_recovery(self) -> bool:
        return self.locator.recovery_dispatch_id is not None


def _locator(metadata: str, app_env: str) -> DispatchLocator:
    try:
        locator = decode_dispatch_metadata(metadata)
    except DispatchMetadataError:
        raise AdmissionRejectedError(RejectReason.INVALID_METADATA) from None
    if locator.environment != app_env:
        raise AdmissionRejectedError(RejectReason.ENVIRONMENT_MISMATCH)
    return locator


def _check_session(record: SessionRecord, locator: DispatchLocator, now: datetime) -> None:
    if (
        record.environment.value != locator.environment
        or record.agent_config_id != locator.agent_config_id
        or record.correlation_id != locator.correlation_id
    ):
        raise AdmissionRejectedError(RejectReason.LOCATOR_MISMATCH)
    if record.termination_request is not None:
        raise AdmissionRejectedError(RejectReason.TERMINATION_REQUESTED)
    if locator.recovery_dispatch_id is not None:
        _check_recovery(record, locator.recovery_dispatch_id, now)
    elif record.status is not SessionStatus.CONNECTING or record.recovery_authorization is not None:
        raise AdmissionRejectedError(RejectReason.NOT_CLAIMABLE)
    if record.has_live_worker(now):
        raise AdmissionRejectedError(RejectReason.WORKER_ALREADY_ASSIGNED)
    if record.maximum_duration_reached(now):
        raise AdmissionRejectedError(RejectReason.MAXIMUM_DURATION_REACHED)


def _check_recovery(record: SessionRecord, recovery_dispatch_id: str, now: datetime) -> None:
    """A replacement job must match the stored authorization (not its ownership lease)."""
    authorization = record.recovery_authorization
    if (
        record.status is not SessionStatus.ACTIVE
        or authorization is None
        or authorization.recovery_dispatch_id != recovery_dispatch_id
        or authorization.deadline_passed(now)
    ):
        raise AdmissionRejectedError(RejectReason.NOT_CLAIMABLE)


def _check_config(config: AgentConfig | None, record: SessionRecord) -> AgentConfig:
    usable = (
        config is not None
        and config.config_checksum == record.config_checksum
        and config.version == record.agent_config_version
        and config.verify_checksum()
        and config.transport.provider == LIVEKIT_TRANSPORT
        and record.recording_mode == "off"
        and record.channel == "browser"
    )
    if not usable or config is None:
        raise AdmissionRejectedError(RejectReason.CONFIGURATION_UNUSABLE)
    return config


async def admit_job(
    *,
    metadata: str,
    room_name: str,
    sessions: SessionReader,
    configs: ConfigReader,
    app_env: str,
    now: datetime,
) -> JobAdmission:
    """Validate a job request; raises :class:`AdmissionRejectedError` with a safe code."""
    locator = _locator(metadata, app_env)
    record = await sessions.get(locator.session_id)
    if record is None:
        raise AdmissionRejectedError(RejectReason.SESSION_NOT_FOUND)
    _check_session(record, locator, now)
    binding = record.transport
    if binding is None or binding.external_room_id != room_name:
        raise AdmissionRejectedError(RejectReason.ROOM_MISMATCH)
    if binding.agent_participant_id is None:
        raise AdmissionRejectedError(RejectReason.ROOM_MISMATCH)
    config = _check_config(await configs.get(record.agent_config_id), record)
    return JobAdmission(
        locator=locator,
        record=record,
        config=config,
        agent_identity=binding.agent_participant_id,
        browser_identity=binding.browser_participant_id,
        room_name=room_name,
    )
