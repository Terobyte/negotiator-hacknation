from __future__ import annotations

from collections.abc import Callable
from threading import RLock
import logging

from negotiator.core.contracts import BusEvent

Subscriber = Callable[[BusEvent], None]
logger = logging.getLogger(__name__)


class EventBus:
    """Synchronous process-local bus for control events (never audio frames)."""

    def __init__(self) -> None:
        self._by_kind: dict[str, list[Subscriber]] = {}
        self._all: list[Subscriber] = []
        self._lock = RLock()

    def subscribe(self, kind: str, subscriber: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._by_kind.setdefault(kind, []).append(subscriber)
        return lambda: self._unsubscribe(kind, subscriber)

    def subscribe_all(self, subscriber: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._all.append(subscriber)
        return lambda: self._unsubscribe(None, subscriber)

    def publish(self, event: BusEvent) -> None:
        with self._lock:
            subscribers = (*self._all, *self._by_kind.get(event.kind, ()))
        first_error: Exception | None = None
        for subscriber in subscribers:
            try:
                subscriber(event)
            except Exception as exc:  # complete fan-out, then preserve fail-fast semantics
                if first_error is None:
                    first_error = exc
                else:
                    logger.error("secondary EventBus subscriber failed: %s", exc, exc_info=exc)
        if first_error is not None:
            raise first_error

    def _unsubscribe(self, kind: str | None, subscriber: Subscriber) -> None:
        with self._lock:
            target = self._all if kind is None else self._by_kind.get(kind, [])
            if subscriber in target:
                target.remove(subscriber)
