"""Deterministic lifecycle event identity (docs/02 §9 "a duplicate delivery reuses the same
``event_id``").

Each control-plane lifecycle event type occurs at most once per session, so
its ``event_id`` is derived from ``(session_id, event_type)``. A retried
append, a background outbox retry, and a reconciler repair therefore all
produce the same ID and the store deduplicates them.
"""

from __future__ import annotations

import uuid
from typing import Final

from voice_agent.contracts.events import EventType

LIFECYCLE_EVENT_NAMESPACE: Final = uuid.UUID("5d7c1f0a-7a4e-4b0e-9a59-2f1f0e8c6a11")


def lifecycle_event_id(session_id: str, event_type: EventType) -> str:
    return str(uuid.uuid5(LIFECYCLE_EVENT_NAMESPACE, f"{session_id}:{event_type.value}"))
