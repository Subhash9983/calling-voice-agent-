"""Decision 070 hard daily spend cap, checked before a new session is created.

Only ``APP_DEPLOYMENT_MODE=remote_limited_sharing`` consults spend; local
mode returns immediately and never reads cost evidence. In remote mode the
UTC-day recorded attempt cost (``costing.daily_spend``) is compared with
``APP_DAILY_SPEND_CAP_INR``; at or above it, creation is refused with the
existing ``RATE_LIMITED`` code (docs/04 §19: "a safe application rate limit")
and a fixed message. The cap and the spend figures never leave the server.

The check fails closed: a missing reader, a truncated read, or unconvertible
evidence refuses creation; a store timeout is the usual safe 503. It runs
at creation time only, so an already-running session may finish past the cap
(bounded by the configuration's maximum session duration).
"""

from __future__ import annotations

import logging
from typing import Final, NoReturn

from voice_agent.control_api.errors import ApiError, ErrorCode
from voice_agent.control_api.runtime import ControlPlaneRuntime
from voice_agent.control_api.structured_logging import get_logger, log_event
from voice_agent.costing.daily_spend import daily_cap_reached, utc_day_start

DAILY_BUDGET_MESSAGE: Final = "The assistant is temporarily unavailable; please try again later."
# Far above a capped day's evidence (a few lines per attempt); more fails closed.
MAX_DAILY_LINES: Final = 5000


def daily_budget_reached() -> ApiError:
    return ApiError(ErrorCode.RATE_LIMITED, DAILY_BUDGET_MESSAGE, retryable=False)


async def ensure_daily_budget(runtime: ControlPlaneRuntime) -> None:
    """Raise the safe refusal when today's recorded spend reached the cap."""
    settings = runtime.require_settings()
    if not settings.is_remote_limited_sharing:
        return
    reader = runtime.require_stores().spend
    if reader is None:
        _refuse("spend_reader_unavailable")
    since = utc_day_start(runtime.clock.utc_now())
    lines = await runtime.bounded(reader.operation_entries_since(since, limit=MAX_DAILY_LINES + 1))
    truncated = len(lines) > MAX_DAILY_LINES
    if daily_cap_reached(lines, settings.app_daily_spend_cap_inr, truncated=truncated):
        _refuse("evidence_truncated" if truncated else "daily_cap_reached")


def _refuse(reason: str) -> NoReturn:
    # Server-side evidence only: a reason code, never an amount.
    log_event(
        get_logger(),
        logging.WARNING,
        "session.create_refused_daily_budget",
        error_code=ErrorCode.RATE_LIMITED.value,
        reasons=[reason],
    )
    raise daily_budget_reached()
