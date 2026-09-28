"""Versioned 30-day R&D retention calculations (docs/02 §20; docs/16 §13).

Terminal session finalization uses ``voice_sessions.ended_at`` as the
retention anchor; every child record receives ``expires_at = ended_at + 30
days``. ``consent``/``billing``-class session events use the later of that
value and every consent-record expiry of the same session; while any consent
record has no expiry yet their expiry stays unknown. Evaluation execution
evidence uses the terminal run ``ended_at`` as its anchor.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

RD_RETENTION_DAYS: Final = 30
RD_RETENTION: Final = timedelta(days=RD_RETENTION_DAYS)
# Versioned policy labels recorded on documents (docs/02 §6, §20).
RETENTION_POLICY_VERSION: Final = "rd_retention_30d_v1"
PRIVACY_POLICY_VERSION: Final = "rd_privacy_v1"
CONTENT_POLICY_VERSION: Final = "rd_content_v1"
# Scheduled cleanup bound (docs/02 §20; docs/03 §21).
MAX_CLEANUP_BATCH_SESSIONS: Final = 100
# Evaluation maximum-dwell rule (docs/16 §7).
EVALUATION_MAX_DWELL: Final = timedelta(days=30)


class EventRetentionClass(StrEnum):
    OPERATIONAL = "operational"
    DIAGNOSTIC = "diagnostic"
    AUDIT = "audit"
    BILLING = "billing"
    CONSENT = "consent"


# Classes removed by ordinary session cleanup; consent/billing events follow
# the protected consent workflow (docs/02 §9, §20).
SESSION_CLEANUP_EVENT_CLASSES: frozenset[EventRetentionClass] = frozenset(
    {EventRetentionClass.OPERATIONAL, EventRetentionClass.DIAGNOSTIC, EventRetentionClass.AUDIT}
)
PROTECTED_EVENT_CLASSES: frozenset[EventRetentionClass] = frozenset(
    {EventRetentionClass.BILLING, EventRetentionClass.CONSENT}
)


def session_expires_at(ended_at: datetime) -> datetime:
    """R&D expiry of a terminal session and its ordinary children."""
    return ended_at + RD_RETENTION


def protected_event_expires_at(
    ended_at: datetime, consent_expiries: Sequence[datetime | None]
) -> datetime | None:
    """Expiry of ``consent``/``billing`` session events, or ``None`` while unknown."""
    if any(expiry is None for expiry in consent_expiries):
        return None
    known = [expiry for expiry in consent_expiries if expiry is not None]
    return max([session_expires_at(ended_at), *known])


def evaluation_run_expires_at(ended_at: datetime) -> datetime:
    """Execution-evidence expiry shared by a run, its results, and its ratings."""
    return ended_at + RD_RETENTION


def retired_definition_expires_at(
    retired_at: datetime, dependent_expiries: Sequence[datetime]
) -> datetime:
    """Earliest cleanup time of a retired configuration or dataset definition."""
    return max([retired_at + RD_RETENTION, *dependent_expiries])


def dwell_exceeded(status_changed_at: datetime, now: datetime) -> bool:
    return now - status_changed_at >= EVALUATION_MAX_DWELL
