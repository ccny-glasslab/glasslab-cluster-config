"""Tests for the research-command-router dispatch surface.

Drives /dispatch through TestClient with a fake requester that records calls,
so each test asserts both the chat-visible response text and the exact
workflow-api endpoint/method/body the router produces for a command. No test
touches a real backend.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import Settings, _request_json, create_app


def test_workflow_mutation_sends_caller_identity(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{}'

    def fake_urlopen(request, timeout):
        captured['headers'] = dict(request.header_items())
        return Response()

    monkeypatch.setattr('app.main.urllib_request.urlopen', fake_urlopen)
    monkeypatch.setenv('GLASSLAB_WORKFLOW_API_CALLER_NAME', 'research-command-router')
    monkeypatch.setenv('GLASSLAB_WORKFLOW_API_TOKEN', 'router-secret')
    _request_json(Settings(), '/research-sessions', method='POST', body={})

    assert captured['headers']['X-glasslab-caller'] == 'research-command-router'
    assert captured['headers']['X-glasslab-workflow-token'] == 'router-secret'


def test_workflow_read_sends_caller_identity(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{}'

    def fake_urlopen(request, timeout):
        captured['headers'] = dict(request.header_items())
        return Response()

    monkeypatch.setattr('app.main.urllib_request.urlopen', fake_urlopen)
    monkeypatch.setenv('GLASSLAB_WORKFLOW_API_CALLER_NAME', 'research-command-router')
    monkeypatch.setenv('GLASSLAB_WORKFLOW_API_TOKEN', 'router-secret')
    _request_json(Settings(), '/research-sessions/latest')

    assert captured['headers']['X-glasslab-caller'] == 'research-command-router'
    assert captured['headers']['X-glasslab-workflow-token'] == 'router-secret'


@pytest.mark.parametrize('token', ['', '   '])
def test_workflow_mutation_fails_closed_without_caller_token(monkeypatch, token) -> None:
    monkeypatch.setenv('GLASSLAB_WORKFLOW_API_CALLER_NAME', 'research-command-router')
    monkeypatch.setenv('GLASSLAB_WORKFLOW_API_TOKEN', token)

    try:
        _request_json(Settings(), '/research-sessions', method='POST', body={})
    except RuntimeError as exc:
        assert str(exc) == 'workflow API credentials are not configured'
    else:
        raise AssertionError('mutation unexpectedly proceeded without credentials')


def _client(requester):
    return TestClient(create_app(settings=Settings(), requester=requester))


def test_help_command_returns_supported_surface_only() -> None:
    client = _client(lambda *args, **kwargs: ("", {}))
    response = client.post("/dispatch", json={"message": "!help"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["matched"] is True
    assert payload["command"] == "help"
    assert "!new <goal>" in payload["response_text"]
    assert "!decide <keep|discard|revise>" in payload["response_text"]
    assert "Use !help legacy" not in payload["response_text"]
    assert "legacy/debug" not in payload["response_text"]


def test_unsupported_bang_command_returns_deterministic_rejection() -> None:
    client = _client(lambda *args, **kwargs: ("", {}))
    response = client.post("/dispatch", json={"message": "!research something old"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["matched"] is False
    assert "deterministic Glasslab commands" in payload["response_text"]


def test_non_command_turn_returns_deterministic_rejection() -> None:
    client = _client(lambda *args, **kwargs: ("", {}))
    response = client.post("/dispatch", json={"message": "what do you think?"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["matched"] is False
    assert "Use !help" in payload["response_text"]


def test_new_command_creates_session() -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def fake_requester(settings, path, method="GET", body=None):
        calls.append((path, method, body))
        return f"{settings.workflow_api_url}{path}", {
            "session_id": "session-123",
            "title": "Artist Similarity",
        }

    client = _client(fake_requester)
    response = client.post("/dispatch", json={"message": "!new artist similarity metric learning"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["command"] == "new"
    assert calls == [
        (
            "/research-sessions",
            "POST",
            {
                "goal_statement": "artist similarity metric learning",
                "priorities": [],
                "submitted_by": "research-command-router",
            },
        )
    ]


def test_add_command_routes_dataset_intake() -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def fake_requester(settings, path, method="GET", body=None):
        calls.append((path, method, body))
        return f"{settings.workflow_api_url}{path}", {
            "record_type": "dataset",
            "dataset": {"name": "WikiArt", "uri": "https://example.org/wikiart.csv"},
            "current_plan_status": "needs_plan",
        }

    client = _client(fake_requester)
    response = client.post("/dispatch", json={"message": "!add dataset: https://example.org/wikiart.csv"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["command"] == "add"
    assert calls[0][0] == "/research-sessions/latest/intake"
    assert calls[0][2]["dataset_uri"] == "https://example.org/wikiart.csv"
    assert "Current plan status: needs_plan." in payload["response_text"]


def test_plan_check_run_and_decide_dispatch_to_single_backend_endpoints() -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def fake_requester(settings, path, method="GET", body=None):
        calls.append((path, method, body))
        payloads = {
            "/research-sessions/latest/transitions/prepare-current-plan": {
                "design_id": "design-1",
                "workflow_id": "artist-similarity",
                "status": "prepared",
            },
            "/research-sessions/latest/preflight/current-plan": {
                "workflow_id": "artist-similarity",
                "blocking_issues": [],
                "warnings": ["warn"],
            },
            "/research-sessions/latest/transitions/run-happy-path": {
                "run": {"run_id": "run-1", "workflow_id": "artist-similarity"}
            },
            "/research-sessions/latest/decisions/current": {
                "decision": "keep"
            },
        }
        return f"{settings.workflow_api_url}{path}", payloads[path]

    client = _client(fake_requester)

    assert client.post("/dispatch", json={"message": "!plan"}).status_code == 200
    assert client.post("/dispatch", json={"message": "!check"}).status_code == 200
    assert client.post("/dispatch", json={"message": "!run"}).status_code == 200
    assert client.post("/dispatch", json={"message": "!decide keep looks good"}).status_code == 200

    assert calls == [
        ("/research-sessions/latest/transitions/prepare-current-plan", "POST", None),
        ("/research-sessions/latest/preflight/current-plan", "GET", None),
        ("/research-sessions/latest/transitions/run-happy-path", "POST", None),
        (
            "/research-sessions/latest/decisions/current",
            "POST",
            {
                "decision": "keep",
                "note": "looks good",
                "submitted_by": "research-command-router",
            },
        ),
    ]


def test_compare_missing_campaign_returns_compact_message() -> None:
    from fastapi import HTTPException, status

    def fake_requester(settings, path, method="GET", body=None):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No autoresearch campaign yet")

    client = _client(fake_requester)
    response = client.post("/dispatch", json={"message": "!compare"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["matched"] is True
    assert payload["command"] == "compare"
    assert "No autoresearch campaign yet" in payload["response_text"]


def test_next_routes_to_single_advance_endpoint() -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def fake_requester(settings, path, method="GET", body=None):
        calls.append((path, method, body))
        return f"{settings.workflow_api_url}{path}", {
            "drafted_methodology_count": 2,
            "decisions_recorded": 1,
            "launches_started": 1,
        }

    client = _client(fake_requester)
    response = client.post("/dispatch", json={"message": "!next"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["command"] == "next"
    assert calls == [
        ("/research-sessions/latest/transitions/advance-autoresearch", "POST", None)
    ]
