"""Bounded Discord REST failure protection.

Classification, bounded retries, and a circuit breaker for the httpx-based
Discord projection adapter (``DiscordHttpAdapter``). Discord is a replaceable
projection: the engine deliberately swallows adapter failures, so this module
never changes the workflow's failure semantics — it only bounds the damage a
degraded Discord REST edge can do (e.g. Cloudflare 1010) and makes the failure
diagnosable through a sanitized readiness snapshot.

Design notes
------------
- ``classify_response`` maps an httpx response to a frozen category. Cloudflare
  1010 is detected from the response body ("error code: 1010") because live
  observations show 403 responses with that body and no ``cf-error-code``
  header; the header is honored when present.
- Retries are bounded and category-aware: HTTP 429 honors ``Retry-After``
  (capped), 5xx/network use exponential backoff, and 401/403/1010 are never
  retried. A total-sleep budget caps the added wall-clock time per guarded
  execution.
- The circuit opens after a run of consecutive terminal failures of any
  category; while open, guarded calls fail fast with ``DiscordCircuitOpen``
  and zero network attempts; a single half-open probe may pass after the
  cooldown and reopens with a fresh cooldown on failure.
- Every emitted artifact (``snapshot()``, transition logging) is sanitized:
  no tokens, no URLs containing interaction tokens, no message content, and
  no response bodies.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NoReturn, TypeVar

import httpx

logger = logging.getLogger(__name__)

CATEGORY_OK = "ok"
CATEGORY_RATE_LIMITED = "rate_limited"
CATEGORY_UNAUTHORIZED = "unauthorized"
CATEGORY_BLOCKED = "blocked"
CATEGORY_CLOUDFLARE_1010 = "cloudflare_1010"
CATEGORY_SERVER_ERROR = "server_error"
CATEGORY_NETWORK = "network"
CATEGORY_CIRCUIT_OPEN = "circuit_open"

STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_HALF_OPEN = "half_open"

# Terminal failure categories: any of these advances the circuit's consecutive
# failure counter. Retryable transients are absorbed by retries first.
TERMINAL_FAILURE_CATEGORIES = frozenset(
    {
        CATEGORY_RATE_LIMITED,
        CATEGORY_UNAUTHORIZED,
        CATEGORY_BLOCKED,
        CATEGORY_CLOUDFLARE_1010,
        CATEGORY_SERVER_ERROR,
        CATEGORY_NETWORK,
    }
)

# Never retried: they represent a persistent block or a credential problem.
NON_RETRYABLE_CATEGORIES = frozenset(
    {CATEGORY_UNAUTHORIZED, CATEGORY_BLOCKED, CATEGORY_CLOUDFLARE_1010}
)

_RETRYABLE_CATEGORIES = frozenset(
    {CATEGORY_RATE_LIMITED, CATEGORY_SERVER_ERROR, CATEGORY_NETWORK}
)

_BODY_SCAN_LIMIT = 4096
_CLOUDFLARE_1010_MARKERS = ("error code: 1010", "cf-error-code: 1010")


@dataclass(frozen=True)
class DiscordRestOutcome:
    """Sanitized classification of one guarded REST attempt."""

    category: str
    status_code: int | None = None
    retries: int = 0
    # Parsed Retry-After (seconds) for rate_limited outcomes; never rendered.
    retry_after: float | None = None


class DiscordRestPolicy:
    """Bounded retry and circuit parameters.

    All time sources are injectable so tests run without real sleeps or clocks.
    """

    def __init__(
        self,
        *,
        circuit_open_failures: int = 3,
        cooldown_seconds: float = 60.0,
        max_429_retries: int = 3,
        max_transient_retries: int = 3,
        retry_after_cap_seconds: float = 30.0,
        base_backoff_seconds: float = 1.0,
        total_sleep_budget_seconds: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if circuit_open_failures < 1:
            raise ValueError("circuit_open_failures must be >= 1")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be > 0")
        if max_429_retries < 0 or max_transient_retries < 0:
            raise ValueError("retry counts must be >= 0")
        if retry_after_cap_seconds <= 0:
            raise ValueError("retry_after_cap_seconds must be > 0")
        if base_backoff_seconds <= 0:
            raise ValueError("base_backoff_seconds must be > 0")
        if total_sleep_budget_seconds <= 0:
            raise ValueError("total_sleep_budget_seconds must be > 0")
        self.circuit_open_failures = circuit_open_failures
        self.cooldown_seconds = cooldown_seconds
        self.max_429_retries = max_429_retries
        self.max_transient_retries = max_transient_retries
        self.retry_after_cap_seconds = retry_after_cap_seconds
        self.base_backoff_seconds = base_backoff_seconds
        self.total_sleep_budget_seconds = total_sleep_budget_seconds
        self.sleep = sleep
        self.monotonic = monotonic


class DiscordCircuitOpen(RuntimeError):
    """Raised when the circuit is open (or a half-open probe is in flight).

    Raised before any network I/O and never recorded as a failure, so the
    circuit can recover via the next probe.
    """


def classify_response(response: httpx.Response) -> DiscordRestOutcome:
    """Map an httpx response to a sanitized outcome category."""
    status = response.status_code
    if 200 <= status < 300:
        return DiscordRestOutcome(category=CATEGORY_OK, status_code=status)
    if status == 429:
        retry_after = _parse_retry_after(response)
        return DiscordRestOutcome(
            category=CATEGORY_RATE_LIMITED,
            status_code=status,
            retry_after=retry_after,
        )
    if status == 401:
        return DiscordRestOutcome(category=CATEGORY_UNAUTHORIZED, status_code=status)
    if status == 403:
        if _is_cloudflare_1010(response):
            return DiscordRestOutcome(
                category=CATEGORY_CLOUDFLARE_1010, status_code=status
            )
        return DiscordRestOutcome(category=CATEGORY_BLOCKED, status_code=status)
    if 500 <= status < 600:
        return DiscordRestOutcome(category=CATEGORY_SERVER_ERROR, status_code=status)
    return DiscordRestOutcome(category=CATEGORY_BLOCKED, status_code=status)


def classify_exception(exc: Exception) -> DiscordRestOutcome:
    """Map a transport exception to the network outcome category."""
    if isinstance(exc, httpx.HTTPError):
        return DiscordRestOutcome(category=CATEGORY_NETWORK)
    return DiscordRestOutcome(category=CATEGORY_NETWORK)


def _parse_retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _is_cloudflare_1010(response: httpx.Response) -> bool:
    header = (response.headers.get("cf-error-code") or "").strip()
    if header == "1010":
        return True
    try:
        body = response.text[:_BODY_SCAN_LIMIT].lower()
    except Exception:
        return False
    return any(marker in body for marker in _CLOUDFLARE_1010_MARKERS)


def is_retryable(category: str) -> bool:
    return category in _RETRYABLE_CATEGORIES


class DiscordRestCircuit:
    """Thread-safe circuit breaker around Discord REST projections."""

    def __init__(self, *, policy: DiscordRestPolicy | None = None) -> None:
        self.policy = policy or DiscordRestPolicy()
        self._lock = threading.Lock()
        self._state = STATE_CLOSED
        self._consecutive_failures = 0
        self._total_failures = 0
        self._total_successes = 0
        self._last_outcome: DiscordRestOutcome | None = None
        self._open_at: float | None = None
        self._half_open_in_flight = False

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def check(self) -> None:
        """Raise ``DiscordCircuitOpen`` when no attempt may run.

        In the open state, transitions to half-open once the cooldown has
        elapsed, allowing exactly one probe (the caller that passes this
        check becomes the probe).
        """
        with self._lock:
            now = self.policy.monotonic()
            if self._state == STATE_OPEN:
                if self._open_at is None or now - self._open_at < self.policy.cooldown_seconds:
                    raise DiscordCircuitOpen()
                self._state = STATE_HALF_OPEN
                self._half_open_in_flight = True
                return
            if self._state == STATE_HALF_OPEN:
                if self._half_open_in_flight:
                    raise DiscordCircuitOpen()
                self._half_open_in_flight = True
                return

    def record(self, outcome: DiscordRestOutcome) -> None:
        """Record an observed outcome and advance the state machine."""
        with self._lock:
            self._last_outcome = outcome
            if outcome.category == CATEGORY_OK:
                self._total_successes += 1
                self._consecutive_failures = 0
                if self._state == STATE_HALF_OPEN:
                    self._state = STATE_CLOSED
                    self._half_open_in_flight = False
                return
            if outcome.category not in TERMINAL_FAILURE_CATEGORIES:
                return
            self._total_failures += 1
            self._consecutive_failures += 1
            if self._state == STATE_HALF_OPEN:
                # The single probe failed: reopen with a fresh cooldown.
                self._state = STATE_OPEN
                self._open_at = self.policy.monotonic()
                self._half_open_in_flight = False
                logger.warning(
                    "discord_rest circuit reopened after failed half-open probe "
                    "category=%s status=%s",
                    outcome.category,
                    outcome.status_code,
                )
                return
            if self._consecutive_failures >= self.policy.circuit_open_failures:
                self._state = STATE_OPEN
                self._open_at = self.policy.monotonic()
                logger.warning(
                    "discord_rest circuit opened after %d consecutive failures "
                    "category=%s status=%s",
                    self._consecutive_failures,
                    outcome.category,
                    outcome.status_code,
                )

    def snapshot(self) -> dict[str, Any]:
        """Sanitized, JSON-serializable state for readiness surfaces."""
        with self._lock:
            now = self.policy.monotonic()
            cooldown_remaining = None
            if self._state == STATE_OPEN and self._open_at is not None:
                remaining = self.policy.cooldown_seconds - (now - self._open_at)
                cooldown_remaining = max(0.0, remaining)
            return {
                "state": self._state,
                "consecutive_failures": self._consecutive_failures,
                "total_failures": self._total_failures,
                "total_successes": self._total_successes,
                "last_outcome_category": (
                    self._last_outcome.category if self._last_outcome else None
                ),
                "last_outcome_status_code": (
                    self._last_outcome.status_code if self._last_outcome else None
                ),
                "cooldown_remaining_seconds": cooldown_remaining,
            }


T = TypeVar("T")


def _default_raise_failure(response: httpx.Response) -> NoReturn:
    response.raise_for_status()
    raise AssertionError("raise_for_status did not raise")  # pragma: no cover


def execute_guarded(
    *,
    circuit: DiscordRestCircuit,
    policy: DiscordRestPolicy | None = None,
    attempt: Callable[[], httpx.Response],
    raise_failure: Callable[[httpx.Response], None] = _default_raise_failure,
) -> httpx.Response:
    """Run one guarded REST attempt with bounded, category-aware retries.

    - While the circuit is open (or a half-open probe is in flight),
      ``DiscordCircuitOpen`` is raised before any network I/O.
    - Retryable outcomes (429 with Retry-After, 5xx, network) retry within the
      policy bounds and the total-sleep budget; the final failure is raised as
      the caller's usual exception (``raise_failure`` for HTTP statuses, the
      original transport exception for network failures).
    - Non-retryable outcomes (401/403/1010) are never retried and advance the
      circuit immediately.
    - Every terminal outcome (and every success) is recorded in the circuit.
    """
    policy = policy or circuit.policy
    circuit.check()
    budget = policy.total_sleep_budget_seconds
    retries_429 = 0
    retries_transient = 0
    while True:
        try:
            response = attempt()
        except httpx.HTTPError as exc:
            outcome = classify_exception(exc)
            if (
                is_retryable(outcome.category)
                and retries_transient < policy.max_transient_retries
                and budget > 0
            ):
                delay = _transient_backoff(policy, retries_transient)
                if delay > budget:
                    circuit.record(outcome)
                    raise
                policy.sleep(delay)
                budget -= delay
                retries_transient += 1
                continue
            circuit.record(outcome)
            raise
        outcome = classify_response(response)
        if outcome.category == CATEGORY_OK:
            circuit.record(outcome)
            return response
        if (
            is_retryable(outcome.category)
            and budget > 0
        ):
            if outcome.category == CATEGORY_RATE_LIMITED:
                if retries_429 >= policy.max_429_retries:
                    circuit.record(outcome)
                    raise_failure(response)
                    return response
                delay = _retry_delay(policy, outcome, retries_429)
                retries_429 += 1
            else:
                if retries_transient >= policy.max_transient_retries:
                    circuit.record(outcome)
                    raise_failure(response)
                    return response
                delay = _transient_backoff(policy, retries_transient)
                retries_transient += 1
            if delay > budget:
                circuit.record(outcome)
                raise_failure(response)
                return response
            policy.sleep(delay)
            budget -= delay
            continue
        circuit.record(outcome)
        raise_failure(response)
        return response


def _retry_delay(
    policy: DiscordRestPolicy, outcome: DiscordRestOutcome, retries: int
) -> float:
    if outcome.category == CATEGORY_RATE_LIMITED:
        retry_after = outcome.retry_after
        if retry_after is None:
            return policy.base_backoff_seconds
        return min(retry_after, policy.retry_after_cap_seconds)
    return _transient_backoff(policy, retries)


def _transient_backoff(policy: DiscordRestPolicy, retries: int) -> float:
    return policy.base_backoff_seconds * (2**retries)