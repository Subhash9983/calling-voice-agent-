"""Reportable 30-day R&D retention evidence (docs/02 §20; WP11).

What is scheduled, what is due, and whether any child record of a terminal
session is still unscheduled. Deletion evidence comes from the bounded
cleanup job's verified per-collection counts (``CleanupRunReport``); both
reports can be written to a local evidence file (``outputs/``) so they are
themselves durable without a new database collection.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

MAX_GAP_SAMPLE: Final = 20


@dataclass(frozen=True, slots=True)
class RetentionOverview:
    environment: str
    policy_version: str
    retention_days: int
    checked_at: datetime
    terminal_sessions: int
    scheduled_sessions: int
    unscheduled_terminal_sessions: int
    due_sessions: int
    next_due_at: datetime | None
    sampled_sessions: int
    unscheduled_children: Mapping[str, int] = field(default_factory=dict)
    sessions_with_gaps: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Every terminal session and every sampled child carries its expiry."""
        return self.unscheduled_terminal_sessions == 0 and not any(
            self.unscheduled_children.values()
        )

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "policy_version": self.policy_version,
            "retention_days": self.retention_days,
            "checked_at": self.checked_at.isoformat(),
            "terminal_sessions": self.terminal_sessions,
            "scheduled_sessions": self.scheduled_sessions,
            "unscheduled_terminal_sessions": self.unscheduled_terminal_sessions,
            "due_sessions": self.due_sessions,
            "next_due_at": None if self.next_due_at is None else self.next_due_at.isoformat(),
            "sampled_sessions": self.sampled_sessions,
            "unscheduled_children": dict(sorted(self.unscheduled_children.items())),
            "sessions_with_gaps": list(self.sessions_with_gaps[:MAX_GAP_SAMPLE]),
            "complete": self.complete,
        }
