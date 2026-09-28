"""Hierarchical cancellation scopes (docs/01 §15).

Session cancellation cancels every turn and operation; turn cancellation
cancels its operations; operation cancellation affects only one request.
Scopes are signals: authoritative output rejection is the generation fence.
"""

from __future__ import annotations

from collections.abc import Callable


class CancelledScopeError(RuntimeError):
    """Raised by :meth:`CancellationScope.raise_if_cancelled`."""


class CancellationScope:
    def __init__(self, name: str, *, parent: CancellationScope | None = None) -> None:
        self.name = name
        self._parent = parent
        self._children: list[CancellationScope] = []
        self._callbacks: list[Callable[[str], None]] = []
        self._reason: str | None = None

    @property
    def is_cancelled(self) -> bool:
        return self._reason is not None

    @property
    def reason(self) -> str | None:
        return self._reason

    def child(self, name: str) -> CancellationScope:
        scope = CancellationScope(name, parent=self)
        if self._reason is not None:
            scope.cancel(self._reason)
        else:
            self._children.append(scope)
        return scope

    def on_cancel(self, callback: Callable[[str], None]) -> None:
        if self._reason is not None:
            callback(self._reason)
            return
        self._callbacks.append(callback)

    def cancel(self, reason: str) -> None:
        if self._reason is not None:
            return
        self._reason = reason
        callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback(reason)
        children, self._children = self._children, []
        for child in children:
            child.cancel(reason)

    def raise_if_cancelled(self) -> None:
        if self._reason is not None:
            raise CancelledScopeError(f"{self.name} cancelled: {self._reason}")
