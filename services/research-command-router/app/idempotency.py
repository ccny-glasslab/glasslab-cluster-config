"""Process-local idempotency for state-changing dispatch commands.

The research-command-router is a stateless compatibility shim: durable state and
policy live in workflow-api. A redelivered chat message, however, would
double-execute a state-changing command (!new/!add/!plan/!run/!next/!decide).
This store keeps a bounded, TTL'd record of the response produced for each
caller-supplied inbound message id so a redelivered message is replayed as a
no-op instead of being forwarded to workflow-api again.

The cache is deliberately process-local: the router runs as a single replica
(see kubeadm/glasslab-v2/research-command-router/10-deployment.yaml), so an
in-process cache is sufficient. It is not a durable store and makes no
cross-pod guarantee.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable


class IdempotencyStore:
    """Bounded TTL cache that runs each live key's work at most once.

    ``reserve`` plus ``complete``/``abandon`` form a claim protocol, so two
    concurrent deliveries of the same message id cannot both execute the
    backend call: the second sees the key as already claimed or completed and
    is told to replay instead.
    """

    def __init__(
        self,
        *,
        max_entries: int = 1024,
        ttl_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._in_flight: set[str] = set()

    def reserve(self, key: str) -> tuple[bool, Any | None]:
        """Claim ``key`` for the caller.

        Returns ``(False, None)`` when the caller now owns the key and must
        finish with ``complete`` (success) or ``abandon`` (failure). Returns
        ``(True, response)`` when the key was already completed (``response`` is
        the stored value) or is already executing (``response`` is ``None``); in
        both cases the caller must treat the delivery as a replay and not run
        the work again.
        """
        with self._lock:
            completed = self._live_entry(key)
            if completed is not None:
                return True, completed
            if key in self._in_flight:
                return True, None
            self._in_flight.add(key)
            return False, None

    def complete(self, key: str, response: Any) -> None:
        """Record a finished claim and evict the oldest entry past the bound."""
        with self._lock:
            self._in_flight.discard(key)
            self._entries[key] = (self._clock(), response)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def abandon(self, key: str) -> None:
        """Release an unfinished claim so a later retry can execute."""
        with self._lock:
            self._in_flight.discard(key)

    def _live_entry(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, response = entry
        if self._clock() - stored_at > self._ttl_seconds:
            del self._entries[key]
            return None
        return response
