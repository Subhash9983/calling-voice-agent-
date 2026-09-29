"""LiveKit agent-server glue: request handling and the per-job entrypoint (docs/06 §7).

- :func:`handle_request` accepts only the approved named room-level job: it
  rejects recording-enabled or foreign jobs, runs durable admission, and
  calls ``req.accept(identity=<opaque agent_participant_id>)``;
- :func:`run_job` re-admits against fresh durable state inside the job's own
  loop, connects the room gateway (listeners first, audio-only subscription),
  verifies the local identity, and runs :class:`WorkerSessionRunner`.

Credentials come only from the WP3 loader and are passed explicitly to the
SDK; nothing here logs a URL, key, token, or secret.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol

from voice_agent.agent_worker.admission import (
    AdmissionRejectedError,
    JobAdmission,
    RejectReason,
    admit_job,
)
from voice_agent.agent_worker.media_check import MediaMode
from voice_agent.agent_worker.session_runner import (
    RunResult,
    WorkerSessionRunner,
    WorkerStores,
)
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.worker_lease import WorkerClaim
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.documents.event import EventWriteContext
from voice_agent.persistence.mongodb.repositories.agent_configs import MongoAgentConfigRepository
from voice_agent.persistence.mongodb.repositories.events import MongoSessionEventLog
from voice_agent.persistence.mongodb.repositories.leases import MongoWorkerLeaseRepository
from voice_agent.persistence.mongodb.repositories.sessions import MongoSessionRecordRepository
from voice_agent.persistence.mongodb.repositories.worker_sessions import (
    MongoWorkerSessionRepository,
)
from voice_agent.ports.transport import SessionTransportPort
from voice_agent.ports.transport_control import TransportControl
from voice_agent.security.settings import BootstrapSettings
from voice_agent.transport_adapters.livekit.control import LiveKitTransportControl
from voice_agent.transport_adapters.livekit.rtc_binding import RtcRoomGateway, SoxResampler
from voice_agent.transport_adapters.livekit.session import LiveKitSessionTransport

WORKER_SERVICE_VERSION: Final = "0.6.0"
# The SDK auto-rejects a job request not answered within 7.5 s.
ADMISSION_RETRY_S: Final = 5.0
ADMISSION_RETRY_INTERVAL_S: Final = 0.25
TRANSIENT_REJECTIONS: Final = frozenset({RejectReason.NOT_CLAIMABLE, RejectReason.ROOM_MISMATCH})
AGENT_DISPLAY_NAME: Final = "agent"
_LOGGER = logging.getLogger("voice_agent.agent_worker")


class JobRequestLike(Protocol):
    """The parts of ``livekit.agents.JobRequest`` the handler uses."""

    @property
    def job(self) -> Any: ...

    @property
    def agent_name(self) -> str: ...

    async def accept(self, *, name: str = "", identity: str = "") -> None: ...

    async def reject(self, *, terminate: bool = True) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    settings: BootstrapSettings
    media_mode: MediaMode
    worker_instance_id: str


PersistenceFactory = Callable[[BootstrapSettings], MongoPersistence]


def new_worker_instance_id() -> str:
    """Opaque per-process identity; never a hostname or process command."""
    return f"worker-{uuid.uuid4().hex[:16]}"


def open_persistence(settings: BootstrapSettings) -> MongoPersistence:
    if settings.mongodb_uri is None:  # pragma: no cover - guarded by startup readiness
        raise RuntimeError("MongoDB is not configured")
    persistence = MongoPersistence(settings.mongodb_uri, database_name=settings.mongodb_database)
    persistence.open()
    return persistence


async def _admit_once(
    persistence: MongoPersistence, settings: BootstrapSettings, *, metadata: str, room: str
) -> JobAdmission:
    return await admit_job(
        metadata=metadata,
        room_name=room,
        sessions=MongoSessionRecordRepository(persistence),
        configs=MongoAgentConfigRepository(persistence),
        app_env=settings.app_env.value,
        now=SystemClock().utc_now(),
    )


async def _admit(
    persistence: MongoPersistence,
    settings: BootstrapSettings,
    *,
    metadata: str,
    room: str,
    retry_s: float = ADMISSION_RETRY_S,
) -> JobAdmission:
    """Admit, briefly retrying the create race (dispatch lands before ``connecting``).

    The control API creates the dispatch before it records the transport
    binding, so the job request can arrive while the session is still
    ``created``. Only those transient reasons are retried, within the SDK's
    job-answer window; every other rejection is final.
    """
    attempts = max(1, int(retry_s / ADMISSION_RETRY_INTERVAL_S))
    for attempt in range(attempts):
        try:
            return await _admit_once(persistence, settings, metadata=metadata, room=room)
        except AdmissionRejectedError as rejected:
            if rejected.reason not in TRANSIENT_REJECTIONS or attempt == attempts - 1:
                raise
        await asyncio.sleep(ADMISSION_RETRY_INTERVAL_S)
    raise AdmissionRejectedError(RejectReason.NOT_CLAIMABLE)  # pragma: no cover


def _precheck(job: Any, agent_name: str, settings: BootstrapSettings) -> str | None:
    """Reject foreign or recording-enabled jobs before any durable read (docs/06 §23)."""
    if agent_name != settings.app_agent_name:
        return "agent_name_mismatch"
    if job.enable_recording:
        # LiveKit Cloud agent observability/session recording is not approved
        # (ordinary recording stays off, docs/05 §3); disable it for the project.
        return "recording_enabled"
    return None


async def handle_request(
    request: JobRequestLike,
    config: WorkerConfig,
    *,
    persistence_factory: PersistenceFactory = open_persistence,
    retry_s: float = ADMISSION_RETRY_S,
) -> JobAdmission | None:
    """Accept with the stored opaque agent identity, or reject safely."""
    job = request.job
    _LOGGER.info(
        "worker.job_received",
        extra={"safe_fields": {"enable_recording": bool(job.enable_recording)}},
    )
    precheck = _precheck(job, request.agent_name, config.settings)
    if precheck is not None:
        _LOGGER.warning("worker.job_rejected", extra={"safe_fields": {"reason": precheck}})
        await request.reject()
        return None
    persistence = persistence_factory(config.settings)
    _LOGGER.info("worker.job_requested")
    try:
        admission = await _admit(
            persistence,
            config.settings,
            metadata=job.metadata,
            room=job.room.name,
            retry_s=retry_s,
        )
    except AdmissionRejectedError as rejected:
        _LOGGER.warning("worker.job_rejected", extra={"safe_fields": {"reason": rejected.reason}})
        await request.reject()
        return None
    finally:
        await persistence.close()
    await request.accept(name=AGENT_DISPLAY_NAME, identity=admission.agent_identity)
    _LOGGER.info("worker.job_accepted")
    return admission


def worker_stores(persistence: MongoPersistence, settings: BootstrapSettings) -> WorkerStores:
    clock = SystemClock()
    context = EventWriteContext(
        environment=AgentConfigEnvironment(settings.app_env.value),
        service_version=WORKER_SERVICE_VERSION,
    )
    cleanup: TransportControl | None = None
    if settings.livekit_url and settings.livekit_api_key and settings.livekit_api_secret:
        cleanup = LiveKitTransportControl(
            url=settings.livekit_url,
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
        )
    return WorkerStores(
        sessions=MongoSessionRecordRepository(persistence),
        leases=MongoWorkerLeaseRepository(persistence),
        worker_sessions=lambda token: MongoWorkerSessionRepository(
            persistence, clock=clock, fence=token
        ),
        events=MongoSessionEventLog(persistence, context=context),
        cleanup=cleanup,
        clock=clock,
        ids=UuidIdGenerator(),
    )


class IdentityMismatchError(RuntimeError):
    """The joined local identity is not the session's opaque agent identity."""


def transport_factory(ctx: Any, admission: JobAdmission) -> Callable[[int], SessionTransportPort]:
    """Build the LiveKit session transport for a ``livekit.agents.JobContext``."""
    from livekit.agents import AutoSubscribe

    async def connect() -> None:
        await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
        if ctx.room.local_participant.identity != admission.agent_identity:
            raise IdentityMismatchError("unexpected local participant identity")

    def build(generation: int) -> SessionTransportPort:
        return LiveKitSessionTransport(
            session_id=admission.record.session_id,
            browser_identity=admission.browser_identity,
            agent_identity=admission.agent_identity,
            worker_generation=generation,
            gateway=RtcRoomGateway(ctx.room, connect=connect),
            clock=SystemClock(),
            resampler_factory=SoxResampler,
            reconnect_window_ms=admission.config.timeout_policy.reconnect_window_ms,
        )

    return build


async def run_job(
    ctx: Any,
    config: WorkerConfig,
    *,
    persistence_factory: PersistenceFactory = open_persistence,
) -> RunResult | None:
    """Per-job entrypoint body: fresh admission, then the session runner."""
    persistence = persistence_factory(config.settings)
    stores = worker_stores(persistence, config.settings)
    try:
        try:
            admission = await _admit(
                persistence, config.settings, metadata=ctx.job.metadata, room=ctx.job.room.name
            )
        except AdmissionRejectedError as rejected:
            _LOGGER.warning(
                "worker.job_readmission_failed", extra={"safe_fields": {"reason": rejected.reason}}
            )
            return None
        runner = WorkerSessionRunner(
            stores,
            admission=admission,
            claim=WorkerClaim(
                worker_instance_id=config.worker_instance_id, livekit_job_id=ctx.job.id
            ),
            transport_factory=transport_factory(ctx, admission),
            media_mode=config.media_mode,
        )
        result = await runner.run()
        _LOGGER.info("worker.job_finished", extra={"safe_fields": {"outcome": result.outcome}})
        return result
    finally:
        if stores.cleanup is not None:
            await stores.cleanup.aclose()
        await persistence.close()
