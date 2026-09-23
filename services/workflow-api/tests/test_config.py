"""Fail-closed settings contract for unknown constructor inputs.

``Settings`` used ``extra='ignore'``, so an unknown constructor kwarg (a
misspelled field name or a stale helper argument) was dropped silently.
``extra='forbid'`` makes that a construction error.

``extra='forbid'`` does NOT catch unknown PLAIN environment variables: the
pydantic-settings env source only iterates declared model fields. The ConfigMap
key-set parity guard is the real env/ConfigMap typo guard; this test constrains
the constructor/dotenv surface only.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_reject_unknown_constructor_kwarg() -> None:
    with pytest.raises(ValidationError):
        Settings(not_a_real_setting='surprise')


def test_settings_reject_misspelled_field_kwarg() -> None:
    with pytest.raises(ValidationError):
        Settings(store_backend_typo='postgres')
