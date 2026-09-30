"""One Deepgram stream attempt: connection, time map, timing, counters, and usage.

Every connect attempt (initial or recovery) is a separate provider operation
with its own ``operation_id`` (docs/07 §12). Usage is attached to the
attempt that processed the audio: the provider-reported ``Metadata``
duration when received, otherwise the measured audio actually sent; the
connected duration is recorded as measured evidence only (docs/15 §2.3).
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from voice_agent.contracts.audio import AudioFrame
from voice_agent.contracts.failures import NormalizedFailure
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.stt import (
    MAX_STREAM_COUNTERS,
    AudioWindow,
    SttAttempt,
    SttStreamClosed,
    SttStreamOutcome,
)
from voice_agent.contracts.usage import (
    UsageItem,
    UsageReport,
    UsageReportingStatus,
    UsageSource,
    UsageUnit,
)
from voice_agent.stt_adapters.deepgram.assembly import StreamTimeMap
from voice_agent.stt_adapters.deepgram.connection import DeepgramConnection

MS: Final = Decimal(1000)


@dataclass(eq=False)
class PendingFinalize:
    """One requested turn finalization awaiting ``from_finalize`` (or the 3 s timeout)."""

    stamp: GenerationStamp
    window: AudioWindow | None
    timer: asyncio.Task[None] | None = None
    resolved: bool = False
    sent: bool = False


def _seconds(milliseconds: int) -> Decimal:
    return Decimal(milliseconds) / MS


def attempt_usage(
    *, sent_ms: int, provider_duration_s: float | None, connected_ms: int
) -> UsageReport:
    """Billable transcribed audio plus measured connected time (evidence-only meter)."""
    connected = UsageItem(
        unit=UsageUnit.CONNECTED_AUDIO_SECONDS,
        quantity=_seconds(connected_ms),
        source=UsageSource.MEASURED,
    )
    if provider_duration_s is None:
        transcribed = UsageItem(
            unit=UsageUnit.TRANSCRIBED_AUDIO_SECONDS,
            quantity=_seconds(sent_ms),
            source=UsageSource.MEASURED,
        )
        status = UsageReportingStatus.MEASURED
    else:
        transcribed = UsageItem(
            unit=UsageUnit.TRANSCRIBED_AUDIO_SECONDS,
            quantity=_seconds(round(provider_duration_s * 1000)),
            source=UsageSource.PROVIDER_REPORTED,
        )
        status = UsageReportingStatus.PROVIDER_REPORTED
    return UsageReport(reporting_status=status, items=(transcribed, connected))


@dataclass(eq=False)
class StreamAttempt:
    operation_id: str
    identity: SttAttempt
    stamp: GenerationStamp
    connection: DeepgramConnection | None = None
    stack: AsyncExitStack | None = None
    connect_ms: int | None = None
    connected_at_ms: int | None = None
    first_result_at_ms: int | None = None
    last_sent_at_ms: int = 0
    last_message_at_ms: int = 0
    last_keepalive_at_ms: int = 0
    awaiting_response: bool = False
    first_sent_capture_ms: int | None = None
    last_sent_capture_end_ms: int | None = None
    last_final_capture_end_ms: int | None = None
    provider_duration_s: float | None = None
    request_id: str | None = None
    closing: bool = False
    finished: bool = False
    times: StreamTimeMap = field(default_factory=StreamTimeMap)
    counters: Counter[str] = field(default_factory=Counter)
    tokens: deque[PendingFinalize] = field(default_factory=deque)
    tasks: list[asyncio.Task[None]] = field(default_factory=list)

    async def send_audio(self, frame: AudioFrame, now_ms: int) -> None:
        if self.connection is None:
            raise RuntimeError("the attempt is not connected")
        await self.connection.send_media(frame.pcm)
        self.times.record(captured_at_ms=frame.captured_at_ms, duration_ms=frame.duration_ms)
        if self.first_sent_capture_ms is None:
            self.first_sent_capture_ms = frame.captured_at_ms
        self.last_sent_capture_end_ms = frame.ends_at_ms
        if not self.awaiting_response:
            self.last_message_at_ms = now_ms
        self.awaiting_response = True
        self.last_sent_at_ms = now_ms

    def unconfirmed_interval(self) -> tuple[int, int] | None:
        """Capture interval sent to this attempt with no finalized result yet."""
        end = self.last_sent_capture_end_ms
        if end is None:
            return None
        start = self.last_final_capture_end_ms
        if start is None:
            start = end if self.first_sent_capture_ms is None else self.first_sent_capture_ms
        return (start, end) if end > start else None

    def usage(self, now_ms: int) -> UsageReport:
        connected = 0 if self.connected_at_ms is None else max(now_ms - self.connected_at_ms, 0)
        return attempt_usage(
            sent_ms=self.times.sent_ms,
            provider_duration_s=self.provider_duration_s,
            connected_ms=connected,
        )

    def closed_event(
        self, outcome: SttStreamOutcome, now_ms: int, failure: NormalizedFailure | None = None
    ) -> SttStreamClosed:
        first = self.first_result_at_ms
        connected_at = self.connected_at_ms
        counters = dict(self.counters.most_common(MAX_STREAM_COUNTERS))
        return SttStreamClosed(
            stamp=self.stamp,
            attempt=self.identity,
            outcome=outcome,
            usage=self.usage(now_ms),
            failure=failure,
            connect_ms=self.connect_ms,
            time_to_first_result_ms=(
                None if first is None or connected_at is None else max(first - connected_at, 0)
            ),
            connected_ms=0 if connected_at is None else max(now_ms - connected_at, 0),
            sent_audio_ms=self.times.sent_ms,
            provider_request_id=self.request_id,
            counters=counters,
        )

    async def aclose(self, timeout_s: float) -> None:
        current = asyncio.current_task()
        for task in self.tasks:
            if task is not current:
                task.cancel()
        stack, self.stack = self.stack, None
        if stack is None:
            return
        try:
            async with asyncio.timeout(timeout_s):
                await stack.aclose()
        except Exception:  # the SDK socket may fail in any way while closing
            self.counters["close_errors"] += 1
