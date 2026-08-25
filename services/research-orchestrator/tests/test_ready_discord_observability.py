"""Wiring tests for Discord REST circuit settings, probe, and readiness.

Covers: settings parsing/validation for the new ``discord_rest_*`` fields,
the bounded ``probe_discord_rest`` background probe, and the ``/ready`` body
that exposes gateway vs REST health separately (HTTP stays 200 while the
database and contract are healthy; Discord state is informational only).
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.discord_rest import (
    CATEGORY_CLOUDFLARE_1010,
    CATEGORY_UNAUTHORIZED,
    DiscordRestCircuit,
    DiscordRestOutcome,
    DiscordRestPolicy,
)
from app.main import build_engine, create_app, probe_discord_rest


class TestDiscordRestSettings:
    def test_discord_rest_settings_parse_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("GLASSLAB_ORCHESTRATOR_DISCORD_REST_CIRCUIT_MAX_FAILURES", "5")
        monkeypatch.setenv("GLASSLAB_ORCHESTRATOR_DISCORD_REST_CIRCUIT_COOLDOWN_SECONDS", "120")
        monkeypatch.setenv("GLASSLAB_ORCHESTRATOR_DISCORD_REST_PROBE_INTERVAL_SECONDS", "0")
        settings = Settings()
        assert settings.discord_rest_circuit_max_failures == 5
        assert settings.discord_rest_circuit_cooldown_seconds == 120.0
        assert settings.discord_rest_probe_interval_seconds == 0.0

    def test_discord_rest_settings_defaults(self) -> None:
        settings = Settings()
        assert settings.discord_rest_circuit_max_failures == 3
        assert settings.discord_rest_circuit_cooldown_seconds == 60.0
        assert settings.discord_rest_probe_interval_seconds == 60.0

    def test_discord_rest_settings_validation(self) -> None:
        with pytest.raises(ValueError):
            Settings(discord_rest_circuit_max_failures=0)
        with pytest.raises(ValueError):
            Settings(discord_rest_circuit_cooldown_seconds=0)
        with pytest.raises(ValueError):
            Settings(discord_rest_probe_interval_seconds=-1)


class TestProbeDiscordRest:
    def test_probe_records_success(self) -> None:
        circuit = DiscordRestCircuit(policy=DiscordRestPolicy(sleep=lambda _: None))
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, json={"id": "app"}, request=req)
        )
        probe_discord_rest(circuit=circuit, token="test-token", transport=transport)
        snapshot = circuit.snapshot()
        assert snapshot["total_successes"] == 1
        assert snapshot["state"] == "closed"

    def test_probe_unauthorized_is_recorded_once(self) -> None:
        circuit = DiscordRestCircuit(policy=DiscordRestPolicy(sleep=lambda _: None))
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(401, request=request)

        probe_discord_rest(
            circuit=circuit, token="test-token", transport=httpx.MockTransport(handler)
        )
        assert len(calls) == 1
        assert circuit.snapshot()["last_outcome_category"] == CATEGORY_UNAUTHORIZED

    def test_probe_respects_open_circuit_with_zero_network(self) -> None:
        circuit = DiscordRestCircuit(
            policy=DiscordRestPolicy(circuit_open_failures=1, sleep=lambda _: None)
        )
        circuit.record(
            DiscordRestOutcome(category=CATEGORY_CLOUDFLARE_1010, status_code=403)
        )
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={}, request=request)

        probe_discord_rest(
            circuit=circuit, token="test-token", transport=httpx.MockTransport(handler)
        )
        assert calls == []
        assert circuit.snapshot()["total_failures"] == 1  # fail-fast never recorded


class TestReadyDiscordObservability:
    def _build_app(self, orchestrator_bundle, **overrides):
        settings, store, cluster, runtime, engine = orchestrator_bundle
        settings = settings.model_copy(
            update={
                "discord_enabled": True,
                "discord_bot_token": "test-token",
                "discord_channel_id": "channel-1",
                "discord_rest_probe_interval_seconds": 0,
                **overrides,
            }
        )
        engine = build_engine(settings, runtime=runtime, cluster=cluster)
        return create_app(settings, engine=engine, start_watcher=False)

    def test_ready_reports_blocked_rest_with_1010_reason(self, orchestrator_bundle) -> None:
        app = self._build_app(orchestrator_bundle)
        with TestClient(app) as client:
            circuit = app.state.discord_rest
            assert circuit is not None
            for _ in range(3):
                circuit.record(
                    DiscordRestOutcome(category=CATEGORY_CLOUDFLARE_1010, status_code=403)
                )
            response = client.get("/ready")
            assert response.status_code == 200
            body = response.json()
            assert body["discord_rest"] == "blocked"
            assert body["discord_rest_reason"] == CATEGORY_CLOUDFLARE_1010
            assert body["discord_gateway"] == "disabled"
            assert body["status"] == "ready"

    def test_ready_reports_unknown_rest_before_any_observation(self, orchestrator_bundle) -> None:
        app = self._build_app(orchestrator_bundle)
        with TestClient(app) as client:
            body = client.get("/ready").json()
            assert body["discord_rest"] == "unknown"
            assert body["discord_rest_reason"] is None
            assert body["status"] == "ready"

    def test_ready_reports_disabled_when_discord_off(self, orchestrator_bundle) -> None:
        settings, store, cluster, runtime, engine = orchestrator_bundle
        app = create_app(settings, engine=engine, start_watcher=False)
        with TestClient(app) as client:
            body = client.get("/ready").json()
            assert body["discord_rest"] == "disabled"
            assert body["discord_rest_reason"] is None
            assert body["discord_gateway"] == "disabled"
            assert body["status"] == "ready"

    def test_health_unchanged(self, orchestrator_bundle) -> None:
        app = self._build_app(orchestrator_bundle)
        with TestClient(app) as client:
            body = client.get("/health").json()
            assert set(body) == {"status", "service", "version"}
            assert body["status"] == "ok"