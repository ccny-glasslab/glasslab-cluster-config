"""Fail-closed tests for credentials required by agent-api settings."""

import pytest
from pydantic import ValidationError

from app.config import Settings


VALID_QWEN_API_KEY = "fixture-qwen-key"
VALID_API_TOKEN = "fixture-agent-token"


def test_settings_require_qwen_api_key(monkeypatch) -> None:
    monkeypatch.delenv("GLASSLAB_AGENT_QWEN_API_KEY", raising=False)

    with pytest.raises(ValidationError):
        Settings(api_token=VALID_API_TOKEN, _env_file=None)


@pytest.mark.parametrize("placeholder", ["change-me", "change-me-before-deploy", "<redacted>", "replace-me"])
def test_settings_reject_placeholder_qwen_api_key(placeholder: str) -> None:
    with pytest.raises(ValidationError):
        Settings(qwen_api_key=placeholder, api_token=VALID_API_TOKEN, _env_file=None)


def test_settings_require_api_token(monkeypatch) -> None:
    monkeypatch.delenv("GLASSLAB_AGENT_API_TOKEN", raising=False)

    with pytest.raises(ValidationError):
        Settings(qwen_api_key=VALID_QWEN_API_KEY, _env_file=None)


@pytest.mark.parametrize("placeholder", ["change-me", "change-me-before-deploy", "<redacted>", "replace-me", "   "])
def test_settings_reject_placeholder_api_token(placeholder: str) -> None:
    with pytest.raises(ValidationError):
        Settings(qwen_api_key=VALID_QWEN_API_KEY, api_token=placeholder, _env_file=None)
