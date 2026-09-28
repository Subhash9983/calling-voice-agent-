"""Domain rule violations."""

from __future__ import annotations


class DomainRuleError(ValueError):
    """A domain invariant was violated."""


class InvalidTransitionError(DomainRuleError):
    """A state transition is not in the approved transition table."""

    def __init__(self, entity: str, from_state: str, to_state: str) -> None:
        super().__init__(f"invalid {entity} transition {from_state} -> {to_state}")
        self.entity = entity
        self.from_state = from_state
        self.to_state = to_state


class IdempotencyConflictError(DomainRuleError):
    """A client request/submission ID was reused with different semantics (docs/04 §6)."""


class LifecycleStateError(DomainRuleError):
    """The entity exists but its lifecycle state prohibits the operation (docs/04 §19)."""
