"""Tests for the Discord REST circuit breaker and failure classification.

Covers the module in ``app/discord_rest.py``: response/exception
classification (including Cloudflare 1010 detection from the response body),
bounded retry semantics (429 Retry-After, 5xx/network backoff, no retry on
401/403/1010, total-sleep budget), and the circuit state machine
(open/reset/half-open single probe/reopen with fresh cooldown, fail-fast that
is never recorded). All sleeps and clock reads are injected so tests run
instantly and deterministically.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app import discord_rest
from app.discord_rest import (
    DiscordCircuitOpen,
    DiscordRestCircuit,
    DiscordRestOutcome,
    DiscordRestPolicy,
    execute_guarded,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CollectingSleep:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _response(status: int, *, body: str = "", headers: dict[str, str] | None = None) -> httpx.Response:
    request = httpx.Request("GET", "https://discord.com/api/v10/gateway")
    return httpx.Response(status, text=body, headers=headers or {}, request=request)


def _sequence_transport(responses: list[httpx.Response]) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if not responses:
            raise AssertionError(f"unexpected extra request: {request.url}")
        return responses.pop(0)

    return httpx.MockTransport(handler), calls


def _attempt(transport: httpx.MockTransport):
    def attempt() -> httpx.Response:
        with httpx.Client(base_url="https://discord.com/api/v10", transport=transport) as client:
            return client.get("/gateway")

    return attempt


def _raise_failure(response: httpx.Response) -> None:
    response.raise_for_status()


class TestClassifyResponse:
    def test_ok(self) -> None:
        outcome = discord_rest.classify_response(_response(200, body='{"url":"wss://gateway.discord.gg"}'))
        assert outcome.category == discord_rest.CATEGORY_OK
        assert outcome.status_code == 200

    def test_rate_limited(self) -> None:
        outcome = discord_rest.classify_response(_response(429, headers={"Retry-After": "5"}))
        assert outcome.category == discord_rest.CATEGORY_RATE_LIMITED
        assert outcome.retry_after == 5.0

    def test_unauthorized(self) -> None:
        outcome = discord_rest.classify_response(_response(401))
        assert outcome.category == discord_rest.CATEGORY_UNAUTHORIZED

    def test_cloudflare_1010_from_body(self) -> None:
        # Live observation: 403 with body "error code: 1010" and no cf-error-code header.
        outcome = discord_rest.classify_response(_response(403, body="error code: 1010\n"))
        assert outcome.category == discord_rest.CATEGORY_CLOUDFLARE_1010

    def test_cloudflare_1010_case_insensitive(self) -> None:
        outcome = discord_rest.classify_response(_response(403, body="ERROR CODE: 1010"))
        assert outcome.category == discord_rest.CATEGORY_CLOUDFLARE_1010

    def test_cloudflare_1010_from_header(self) -> None:
        outcome = discord_rest.classify_response(
            _response(403, headers={"cf-error-code": "1010"})
        )
        assert outcome.category == discord_rest.CATEGORY_CLOUDFLARE_1010

    def test_generic_blocked(self) -> None:
        outcome = discord_rest.classify_response(_response(403, body="Forbidden"))
        assert outcome.category == discord_rest.CATEGORY_BLOCKED

    def test_client_error_400(self) -> None:
        outcome = discord_rest.classify_response(_response(400))
        assert outcome.category == discord_rest.CATEGORY_CLIENT_ERROR
        assert outcome.status_code == 400

    def test_client_error_404(self) -> None:
        outcome = discord_rest.classify_response(_response(404))
        assert outcome.category == discord_rest.CATEGORY_CLIENT_ERROR

    def test_client_error_422(self) -> None:
        outcome = discord_rest.classify_response(_response(422))
        assert outcome.category == discord_rest.CATEGORY_CLIENT_ERROR

    def test_server_error(self) -> None:
        outcome = discord_rest.classify_response(_response(503))
        assert outcome.category == discord_rest.CATEGORY_SERVER_ERROR


class TestClassifyException:
    def test_timeout_is_network(self) -> None:
        exc = httpx.TimeoutException("timed out", request=httpx.Request("GET", "https://discord.com"))
        assert discord_rest.classify_exception(exc).category == discord_rest.CATEGORY_NETWORK

    def test_connect_error_is_network(self) -> None:
        exc = httpx.ConnectError("conn refused", request=httpx.Request("GET", "https://discord.com"))
        assert discord_rest.classify_exception(exc).category == discord_rest.CATEGORY_NETWORK


class TestRetrySemantics:
    def test_429_respects_retry_after(self) -> None:
        sleep = CollectingSleep()
        policy = DiscordRestPolicy(sleep=sleep)
        transport, calls = _sequence_transport(
            [_response(429, headers={"Retry-After": "3"}), _response(200)]
        )
        circuit = DiscordRestCircuit(policy=policy)
        response = execute_guarded(circuit=circuit, policy=policy, attempt=_attempt(transport), raise_failure=_raise_failure)
        assert response.status_code == 200
        assert len(calls) == 2
        assert sleep.sleeps == [3.0]

    def test_429_retry_after_is_capped(self) -> None:
        sleep = CollectingSleep()
        policy = DiscordRestPolicy(sleep=sleep, retry_after_cap_seconds=30.0)
        transport, _ = _sequence_transport(
            [_response(429, headers={"Retry-After": "300"}), _response(200)]
        )
        circuit = DiscordRestCircuit(policy=policy)
        execute_guarded(circuit=circuit, policy=policy, attempt=_attempt(transport), raise_failure=_raise_failure)
        assert sleep.sleeps == [30.0]

    def test_5xx_uses_exponential_backoff(self) -> None:
        sleep = CollectingSleep()
        policy = DiscordRestPolicy(sleep=sleep)
        transport, calls = _sequence_transport([_response(503), _response(502), _response(200)])
        circuit = DiscordRestCircuit(policy=policy)
        execute_guarded(circuit=circuit, policy=policy, attempt=_attempt(transport), raise_failure=_raise_failure)
        assert len(calls) == 3
        assert sleep.sleeps == [1.0, 2.0]

    def test_403_1010_is_never_retried(self) -> None:
        sleep = CollectingSleep()
        policy = DiscordRestPolicy(sleep=sleep)
        transport, calls = _sequence_transport([_response(403, body="error code: 1010\n")])
        circuit = DiscordRestCircuit(policy=policy)
        with pytest.raises(httpx.HTTPStatusError):
            execute_guarded(circuit=circuit, policy=policy, attempt=_attempt(transport), raise_failure=_raise_failure)
        assert len(calls) == 1
        assert sleep.sleeps == []

    def test_network_error_retries_then_propagates_original(self) -> None:
        sleep = CollectingSleep()
        policy = DiscordRestPolicy(sleep=sleep)
        calls: list[str] = []

        def attempt() -> httpx.Response:
            calls.append("attempt")
            raise httpx.ConnectError("boom", request=httpx.Request("GET", "https://discord.com"))

        circuit = DiscordRestCircuit(policy=policy)
        with pytest.raises(httpx.ConnectError):
            execute_guarded(circuit=circuit, policy=policy, attempt=attempt, raise_failure=_raise_failure)
        assert len(calls) == 1 + policy.max_transient_retries
        assert sleep.sleeps == [1.0, 2.0, 4.0]

    def test_total_sleep_budget_caps_retries(self) -> None:
        sleep = CollectingSleep()
        policy = DiscordRestPolicy(sleep=sleep, total_sleep_budget_seconds=3.0)
        transport, calls = _sequence_transport([_response(503), _response(503), _response(503), _response(503)])
        circuit = DiscordRestCircuit(policy=policy)
        with pytest.raises(httpx.HTTPStatusError):
            execute_guarded(circuit=circuit, policy=policy, attempt=_attempt(transport), raise_failure=_raise_failure)
        # budget 3.0 allows sleeps 1.0 + 2.0; the 4th backoff (4.0) exceeds the
        # remaining budget, so the third attempt is terminal -> 3 calls total.
        assert len(calls) == 3
        assert sleep.sleeps == [1.0, 2.0]
        assert sum(sleep.sleeps) <= 3.0


class TestCircuitStateMachine:
    def test_opens_after_n_terminal_failures(self) -> None:
        policy = DiscordRestPolicy(circuit_open_failures=3)
        circuit = DiscordRestCircuit(policy=policy)
        for _ in range(2):
            circuit.record(DiscordRestOutcome(category=discord_rest.CATEGORY_CLOUDFLARE_1010, status_code=403))
            assert circuit.state == discord_rest.STATE_CLOSED
        circuit.record(DiscordRestOutcome(category=discord_rest.CATEGORY_CLOUDFLARE_1010, status_code=403))
        assert circuit.state == discord_rest.STATE_OPEN

    def test_mixed_terminal_categories_count_toward_open(self) -> None:
        policy = DiscordRestPolicy(circuit_open_failures=3)
        circuit = DiscordRestCircuit(policy=policy)
        for category in (
            discord_rest.CATEGORY_SERVER_ERROR,
            discord_rest.CATEGORY_NETWORK,
            discord_rest.CATEGORY_UNAUTHORIZED,
        ):
            circuit.record(DiscordRestOutcome(category=category, status_code=None))
        assert circuit.state == discord_rest.STATE_OPEN

    def test_success_resets_counter(self) -> None:
        policy = DiscordRestPolicy(circuit_open_failures=3)
        circuit = DiscordRestCircuit(policy=policy)
        circuit.record(DiscordRestOutcome(category=discord_rest.CATEGORY_CLOUDFLARE_1010, status_code=403))
        circuit.record(DiscordRestOutcome(category=discord_rest.CATEGORY_OK, status_code=200))
        circuit.record(DiscordRestOutcome(category=discord_rest.CATEGORY_CLOUDFLARE_1010, status_code=403))
        assert circuit.state == discord_rest.STATE_CLOSED

    def test_fail_fast_while_open_raises_and_never_records(self) -> None:
        policy = DiscordRestPolicy(circuit_open_failures=1, cooldown_seconds=60.0)
        circuit = DiscordRestCircuit(policy=policy)
        circuit.record(DiscordRestOutcome(category=discord_rest.CATEGORY_CLOUDFLARE_1010, status_code=403))
        assert circuit.state == discord_rest.STATE_OPEN
        with pytest.raises(DiscordCircuitOpen):
            circuit.check()
        # fail-fast must not advance any counter
        snapshot = circuit.snapshot()
        assert snapshot["total_failures"] == 1
        assert snapshot["consecutive_failures"] == 1

    def test_half_open_probe_success_closes(self) -> None:
        clock = FakeClock()
        policy = DiscordRestPolicy(circuit_open_failures=1, cooldown_seconds=60.0, monotonic=clock)
        circuit = DiscordRestCircuit(policy=policy)
        circuit.record(DiscordRestOutcome(category=discord_rest.CATEGORY_CLOUDFLARE_1010, status_code=403))
        clock.advance(61.0)
        circuit.check()  # transitions to half-open and allows the probe
        assert circuit.state == discord_rest.STATE_HALF_OPEN
        circuit.record(DiscordRestOutcome(category=discord_rest.CATEGORY_OK, status_code=200))
        assert circuit.state == discord_rest.STATE_CLOSED
        assert circuit.snapshot()["consecutive_failures"] == 0

    def test_half_open_probe_failure_reopens_with_fresh_cooldown(self) -> None:
        clock = FakeClock()
        policy = DiscordRestPolicy(circuit_open_failures=1, cooldown_seconds=60.0, monotonic=clock)
        circuit = DiscordRestCircuit(policy=policy)
        circuit.record(DiscordRestOutcome(category=discord_rest.CATEGORY_CLOUDFLARE_1010, status_code=403))
        clock.advance(61.0)
        circuit.check()
        assert circuit.state == discord_rest.STATE_HALF_OPEN
        circuit.record(DiscordRestOutcome(category=discord_rest.CATEGORY_CLOUDFLARE_1010, status_code=403))
        assert circuit.state == discord_rest.STATE_OPEN
        # fresh cooldown: immediately calling check() must still raise
        with pytest.raises(DiscordCircuitOpen):
            circuit.check()
        clock.advance(59.0)
        with pytest.raises(DiscordCircuitOpen):
            circuit.check()
        clock.advance(1.0)
        circuit.check()  # allowed again
        assert circuit.state == discord_rest.STATE_HALF_OPEN

    def test_half_open_allows_single_probe_only(self) -> None:
        clock = FakeClock()
        policy = DiscordRestPolicy(circuit_open_failures=1, cooldown_seconds=60.0, monotonic=clock)
        circuit = DiscordRestCircuit(policy=policy)
        circuit.record(DiscordRestOutcome(category=discord_rest.CATEGORY_CLOUDFLARE_1010, status_code=403))
        clock.advance(61.0)
        circuit.check()
        with pytest.raises(DiscordCircuitOpen):
            circuit.check()  # second caller while probe in flight fails fast

    def test_snapshot_is_sanitized_and_json_serializable(self) -> None:
        policy = DiscordRestPolicy(circuit_open_failures=1)
        circuit = DiscordRestCircuit(policy=policy)
        circuit.record(DiscordRestOutcome(category=discord_rest.CATEGORY_CLOUDFLARE_1010, status_code=403))
        snapshot = circuit.snapshot()
        json.dumps(snapshot)  # must be JSON-serializable
        blob = json.dumps(snapshot)
        for forbidden in ("token", "webhook", "Bot ", "interaction", "content", "body"):
            assert forbidden not in blob.lower()

    def test_policy_validation(self) -> None:
        with pytest.raises(ValueError):
            DiscordRestPolicy(circuit_open_failures=0)
        with pytest.raises(ValueError):
            DiscordRestPolicy(cooldown_seconds=0)
        with pytest.raises(ValueError):
            DiscordRestPolicy(total_sleep_budget_seconds=0)


class TestClientErrorsDoNotOpenCircuit:
    """Ordinary application-level 4xx must never open the global circuit."""

    def test_repeated_400_does_not_open_circuit(self) -> None:
        policy = DiscordRestPolicy(circuit_open_failures=3, sleep=CollectingSleep())
        transport, calls = _sequence_transport(
            [_response(400), _response(400), _response(400), _response(400)]
        )
        circuit = DiscordRestCircuit(policy=policy)
        for _ in range(4):
            with pytest.raises(httpx.HTTPStatusError):
                execute_guarded(
                    circuit=circuit,
                    policy=policy,
                    attempt=_attempt(transport),
                    raise_failure=_raise_failure,
                )
        assert circuit.state == discord_rest.STATE_CLOSED
        assert circuit.snapshot()["consecutive_failures"] == 0
        assert circuit.snapshot()["total_failures"] == 0

    def test_repeated_404_does_not_open_circuit(self) -> None:
        policy = DiscordRestPolicy(circuit_open_failures=1, sleep=CollectingSleep())
        transport, calls = _sequence_transport(
            [_response(404), _response(404), _response(404)]
        )
        circuit = DiscordRestCircuit(policy=policy)
        for _ in range(3):
            with pytest.raises(httpx.HTTPStatusError):
                execute_guarded(
                    circuit=circuit,
                    policy=policy,
                    attempt=_attempt(transport),
                    raise_failure=_raise_failure,
                )
        assert circuit.state == discord_rest.STATE_CLOSED
        assert circuit.snapshot()["consecutive_failures"] == 0

    def test_client_error_is_not_retried(self) -> None:
        sleep = CollectingSleep()
        policy = DiscordRestPolicy(sleep=sleep)
        transport, calls = _sequence_transport([_response(400)])
        circuit = DiscordRestCircuit(policy=policy)
        with pytest.raises(httpx.HTTPStatusError):
            execute_guarded(
                circuit=circuit,
                policy=policy,
                attempt=_attempt(transport),
                raise_failure=_raise_failure,
            )
        assert len(calls) == 1
        assert sleep.sleeps == []

    def test_client_error_observable_in_snapshot_without_circuit_impact(self) -> None:
        policy = DiscordRestPolicy(circuit_open_failures=3, sleep=CollectingSleep())
        circuit = DiscordRestCircuit(policy=policy)
        circuit.record(
            DiscordRestOutcome(category=discord_rest.CATEGORY_CLIENT_ERROR, status_code=400)
        )
        snapshot = circuit.snapshot()
        assert snapshot["state"] == discord_rest.STATE_CLOSED
        assert snapshot["consecutive_failures"] == 0
        assert snapshot["total_failures"] == 0
        assert snapshot["last_outcome_category"] == discord_rest.CATEGORY_CLIENT_ERROR
        assert snapshot["last_outcome_status_code"] == 400
