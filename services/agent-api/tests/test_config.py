"""Fail-closed tests for credentials required by agent-api settings."""

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_require_qwen_api_key(monkeypatch) -> None:
    monkeypatch.delenv("GLASSLAB_AGENT_QWEN_API_KEY", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("placeholder", ["change-me", "change-me-before-deploy", "<redacted>", "replace-me"])
def test_settings_reject_placeholder_qwen_api_key(placeholder: str) -> None:
    with pytest.raises(ValidationError):
        Settings(qwen_api_key=placeholder, _env_file=None)
