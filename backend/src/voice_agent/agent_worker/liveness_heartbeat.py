"""Browser liveness heartbeat for the speech media-modes (docs/06 §11-§12).

The tone/echo media check (:mod:`media_check`) sends a lossy
``va.metrics.v1`` message every interval. The browser treats any agent
message as proof that the agent is alive and shows "recovering" after a few
seconds without one, so a speech-mode session (stt/llm/tts/conversation)
that is merely quiet -- the user has not spoken yet, or is mid-turn --
must send the same heartbeat. It reuses the approved topic, event type
(``transport.quality_updated``), and the existing ``lease_valid_for_ms``
lease-timing hint; there is no new wire contract. Lossy: a lost beat is
replaced by the next one.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Final

from voice_agent.contracts.events import EventEnvelope, EventType, EventVisibility
from voice_agent.contracts.transport import RealtimeTopic
from voice_agent.ports.clock import Clock, IdGenerator
from voice_agent.ports.transport import WorkerTransportPort

WORKER_COMPONENT: Final = "worker"
WORKER_SERVICE: Final = "agent_worker"
_LOGGER = logging.getLogger("voice_agent.agent_worker.heartbeat")


async def browser_heartbeat(
    transport: WorkerTransportPort,
    *,
    session_id: str,
    correlation_id: str,
    clock: Clock,
    ids: IdGenerator,
    lease_hint: Callable[[], int],
    interval_s: float,
) -> None:
    """Send one lossy ``va.metrics.v1`` beat per interval until cancelled."""
    while True:
        await asyncio.sleep(interval_s)
        envelope = EventEnvelope(
            event_id=ids.new_id(),
            event_type=EventType.TRANSPORT_QUALITY_UPDATED,
            occurred_at=clock.utc_now(),
            session_id=session_id,
            correlation_id=correlation_id,
            component=WORKER_COMPONENT,
            producer_service=WORKER_SERVICE,
            visibility=EventVisibility.BROWSER_SAFE,
            payload={"lease_valid_for_ms": lease_hint()},
        )
        try:
            await transport.send_event(RealtimeTopic.METRICS, envelope, reliable=False)
        except Exception as error:  # a lost beat is replaced by the next one
            _LOGGER.warning(
                "worker.heartbeat_send_failed",
                extra={"safe_fields": {"error": type(error).__name__}},
            )
