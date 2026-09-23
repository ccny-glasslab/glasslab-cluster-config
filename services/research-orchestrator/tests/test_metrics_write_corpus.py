"""Differential preflight corpus for the ``metrics.json`` AST checker.

The checker must be *semantics-driven*, not an idiom enumerator: any ``run.py``
shape that writes the required JSON object to ``metrics.json`` is accepted, and
every shape that writes the wrong filename, omits a required root key, or emits
non-JSON is rejected. This module runs the whole corpus from
``tests/metrics_write_corpus.py`` - verbatim frozen-corpus excerpts plus
mechanically generated equivalent/broken shapes - through
:func:`app.preflight._metrics_root_errors` and asserts the outcome, so a valid
shape that regresses to "unrecognized" fails here in seconds, offline.
"""

from __future__ import annotations

import ast

import pytest

from app.preflight import _metrics_root_errors
from metrics_write_corpus import (
    GENERATED_DIR,
    Variant,
    all_variants,
    generated_variants,
    real_variants,
)

_NO_WRITE = 'run.py does not have a statically verifiable JSON write to metrics.json'

_CASES = all_variants()


def _expected_errors(variant: Variant) -> list[str]:
    if variant.expectation == 'accept':
        return []
    if variant.expectation == 'no_write':
        return [_NO_WRITE]
    assert variant.expectation == 'missing_keys', variant.expectation
    return [
        'run.py serializes metrics.json without required root key(s): '
        + ', '.join(variant.missing_keys)
    ]


def _actual_errors(variant: Variant) -> list[str]:
    return _metrics_root_errors(
        ast.parse(variant.source),
        relative='run.py',
        required_metric_keys=list(variant.required_keys),
    )


@pytest.mark.parametrize('variant', _CASES, ids=[v.name for v in _CASES])
def test_corpus_variant_matches_expected_outcome(variant: Variant) -> None:
    assert _actual_errors(variant) == _expected_errors(variant)


def test_corpus_contains_real_and_generated_samples() -> None:
    real = real_variants()
    generated = generated_variants()
    # The frozen corpus supplies the real idioms; the generator supplies the
    # equivalent/broken breadth. Both must be non-trivial.
    assert len(real) >= 6
    assert len(generated) >= 30
    assert {v.category for v in generated} >= {
        'handle',
        'handle_write',
        'inline_handle',
        'write_text',
        'path_open',
        'dumps_variable',
        'atomic',
        'wrong_file',
        'missing_key',
        'non_json',
        'no_write',
    }


def test_every_variant_category_has_both_polarities_or_is_real() -> None:
    # Guard against a corpus that only exercises the positive path.
    expectations = {v.expectation for v in generated_variants()}
    assert expectations == {'accept', 'no_write', 'missing_keys'}
    assert {v.expectation for v in real_variants()} == {'accept'}


def test_generated_fixtures_match_generator() -> None:
    # The committed generated/*.py files are the inspectable corpus; they must
    # be exactly what the generator emits (no hand-edited drift).
    for variant in generated_variants():
        path = GENERATED_DIR / f'{variant.name}.py'
        assert path.is_file(), f'missing generated fixture: {path}'
        assert path.read_text(encoding='utf-8') == variant.source


def test_real_excerpts_are_present_on_disk() -> None:
    for variant in real_variants():
        assert variant.source.strip(), variant.name
        assert 'metrics.json' in variant.source
