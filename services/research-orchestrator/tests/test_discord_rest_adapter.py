"""Adapter integration tests: the REST circuit guards the HTTP projection.

Exercises ``DiscordHttpAdapter`` with an injected circuit and a sequenced
``httpx.MockTransport``: the circuit opens after repeated 403/1010 storms and
fail-fast with zero additional network attempts; 429 honors ``Retry-After``;
5xx exhaustion raises the original exception and opens the circuit; the
webhook path shares the circuit; early-return publish paths bypass it.
"""

from __future__ import annotations

import httpx
import pytest

from app import discord_rest
from app.discord_adapter import DiscordHttpAdapter
from app.discord_rest import (
    DiscordCircuitOpen,
    DiscordRestCircuit,
    DiscordRestPolicy,
)
from app.schemas import EventRecord
from test_discord_rest_circuit import (
    _response,
    _sequence_transport,
    CollectingSleep,
)

# Adapter integration: the circuit guards create_thread / publish / webhook.
# ---------------------------------------------------------------------------

from app.discord_adapter import DiscordHttpAdapter
from app.schemas import EventRecord


def _adapter_with_circuit(
    transport: httpx.MockTransport,
    *,
    webhook_url: str | None = None,
    policy: DiscordRestPolicy | None = None,
) -> tuple[DiscordHttpAdapter, DiscordRestCircuit]:
    circuit = DiscordRestCircuit(policy=policy or DiscordRestPolicy(sleep=CollectingSleep()))
    adapter = DiscordHttpAdapter(
        bot_token="test-token",
        channel_id="channel-1",
        webhook_url=webhook_url,
        transport=transport,
        circuit=circuit,
    )
    return adapter, circuit


def _run_created_event() -> EventRecord:
    return EventRecord(
        sequence_number=1,
        run_id="run-1",
        source="orchestrator",
        event_type="run.created",
        payload={"objective": "test objective"},
    )


class TestAdapterCircuitIntegration:
    def test_create_thread_1010_storm_fail_fast_after_threshold(self) -> None:
        policy = DiscordRestPolicy(
            circuit_open_failures=3,
            cooldown_seconds=60.0,
            sleep=CollectingSleep(),
        )
        responses = [
            _response(403, body="error code: 1010\n"),
            _response(403, body="error code: 1010\n"),
            _response(403, body="error code: 1010\n"),
        ]
        transport, calls = _sequence_transport(responses)
        adapter, circuit = _adapter_with_circuit(transport, policy=policy)
        for _ in range(3):
            with pytest.raises(httpx.HTTPStatusError):
                adapter.create_thread(run_id="run-1", objective="objective")
        assert circuit.state == discord_rest.STATE_OPEN
        # While open: zero additional HTTP attempts.
        with pytest.raises(DiscordCircuitOpen):
            adapter.create_thread(run_id="run-1", objective="objective")
        assert len(calls) == 3
        assert circuit.snapshot()["total_failures"] == 3

    def test_publish_429_honors_retry_after_then_succeeds(self) -> None:
        sleep = CollectingSleep()
        policy = DiscordRestPolicy(sleep=sleep)
        transport, calls = _sequence_transport(
            [
                _response(429, headers={"Retry-After": "2"}),
                _response(200, body='{"id": "message-1"}'),
            ]
        )
        adapter, circuit = _adapter_with_circuit(transport, policy=policy)
        result = adapter.publish(
            thread_id="thread-1",
            status_message_id=None,
            event=_run_created_event(),
        )
        assert result is None
        assert len(calls) == 2
        assert sleep.sleeps == [2.0]
        assert circuit.snapshot()["total_successes"] == 1

    def test_publish_5xx_exhaustion_raises_original_and_opens(self) -> None:
        policy = DiscordRestPolicy(
            circuit_open_failures=3,
            max_transient_retries=0,
            sleep=CollectingSleep(),
        )
        transport, calls = _sequence_transport([_response(503), _response(503), _response(503)])
        adapter, circuit = _adapter_with_circuit(transport, policy=policy)
        for _ in range(3):
            with pytest.raises(httpx.HTTPStatusError):
                adapter.publish(thread_id="thread-1", status_message_id=None, event=_run_created_event())
        assert circuit.state == discord_rest.STATE_OPEN
        assert len(calls) == 3

    def test_webhook_1010_opens_circuit_and_fail_fast(self) -> None:
        policy = DiscordRestPolicy(circuit_open_failures=2, sleep=CollectingSleep())
        responses = [
            _response(403, body="error code: 1010\n"),
            _response(403, body="error code: 1010\n"),
        ]
        transport, calls = _sequence_transport(responses)
        adapter, circuit = _adapter_with_circuit(
            transport, webhook_url="https://discord.com/api/webhooks/1/secret", policy=policy
        )
        for _ in range(2):
            with pytest.raises(RuntimeError, match="Discord webhook returned HTTP 403"):
                adapter.publish(thread_id="thread-1", status_message_id=None, event=_run_created_event())
        assert circuit.state == discord_rest.STATE_OPEN
        with pytest.raises(DiscordCircuitOpen):
            adapter.publish(thread_id="thread-1", status_message_id=None, event=_run_created_event())
        assert len(calls) == 2

    def test_early_return_paths_bypass_circuit(self) -> None:
        transport, calls = _sequence_transport([])
        adapter, circuit = _adapter_with_circuit(transport)
        # thread_id is None: no Discord call, no circuit interaction.
        adapter.publish(thread_id=None, status_message_id="status-1", event=_run_created_event())
        assert calls == []
        assert circuit.snapshot()["total_failures"] == 0
        assert circuit.snapshot()["total_successes"] == 0
