"""Hardening regression tests for signed links.

Two review findings on the link surface:

* The default uvicorn access log records the full ``/links/{token}`` path, so
  the bearer token must be redacted before it reaches the log.
* The base64url decoder must accept only the canonical URL-safe alphabet, so a
  single signature cannot be spelled several ways (or carry a path separator).
"""

from __future__ import annotations

import logging

import pytest

from app.links import _b64url_decode
from app.main import _RedactLinkTokensFilter


def _access_record(target: str) -> logging.LogRecord:
    return logging.LogRecord(
        name='uvicorn.access',
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=('127.0.0.1:1234', 'GET', target, '1.1', 200),
        exc_info=None,
    )


def test_access_log_redacts_the_link_token() -> None:
    record = _access_record('/links/abc.def')
    assert _RedactLinkTokensFilter().filter(record) is True
    assert record.args[2] == '/links/<redacted>'
    # The client address, method, and status survive untouched.
    assert record.args[:2] == ('127.0.0.1:1234', 'GET')
    assert record.args[3:] == ('1.1', 200)


def test_access_log_leaves_other_requests_unchanged() -> None:
    record = _access_record('/runs/abc/events')
    assert _RedactLinkTokensFilter().filter(record) is True
    assert record.args[2] == '/runs/abc/events'


@pytest.mark.parametrize('value', ['ab+cd', 'ab/cd', 'ab=cd'])
def test_non_canonical_base64url_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        _b64url_decode(value)
