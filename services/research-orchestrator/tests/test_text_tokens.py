"""Shared content-aware token estimator (#517).

``app.text_tokens`` is the single dependency-free leaf every caller routes
through. Its estimate is ``max(1, whitespace words, ceil(chars / 3))``: it is
never below the old whitespace word count, counts compact JSON by content
rather than by whitespace, and floors empty/whitespace-only text at one token.
"""

from __future__ import annotations

import app.corpus_rag.retrieval as retrieval
import app.corpus_rag.spans as spans
import app.knowledge_manager as knowledge_manager
import app.text_tokens as text_tokens
from app.prompt_tokens import estimate_prompt_tokens

_SAMPLES = [
    '',
    '   \n\t ',
    'one two three',
    '{"a":1,"b":2}',
    'alpha beta gamma delta epsilon',
    '{"methodologies":[{"name":"baseline","seeds":[1,2,3]}]}',
    'line one\nline two\nline three',
]


def test_empty_and_whitespace_only_text_cost_one_token() -> None:
    assert text_tokens.estimate_tokens('') == 1
    assert text_tokens.estimate_tokens('   \n\t ') == 1


def test_compact_json_is_content_aware_not_a_single_word() -> None:
    payload = '{"a":1,"b":2}'
    assert len(payload.split()) == 1
    # 13 characters at 3 chars/token -> ceil(13 / 3) == 5, which wins.
    assert text_tokens.estimate_tokens(payload) == 5
    assert text_tokens.estimate_tokens(payload) > len(payload.split())


def test_estimate_never_below_word_count_and_always_at_least_one() -> None:
    for sample in _SAMPLES:
        estimate = text_tokens.estimate_tokens(sample)
        assert estimate >= len(sample.split())
        assert estimate >= 1


def test_character_floor_beats_the_word_count_when_longer() -> None:
    # 13 characters, three whitespace words -> the character floor wins.
    assert text_tokens.estimate_tokens('one two three') == -(-13 // 3) == 5


def test_estimate_prompt_tokens_delegates_to_the_shared_estimator() -> None:
    for sample in _SAMPLES:
        assert estimate_prompt_tokens(sample) == text_tokens.estimate_tokens(sample)


def test_estimate_prompt_tokens_is_monotonic_under_growth() -> None:
    # Rotation only ever grows a prompt; the estimator must never shrink.
    previous = 0
    growing = ''
    for chunk in ('{"seed":1}', ' {"seed":2}', ' {"seed":3}', ' {"seed":4}'):
        growing += chunk
        current = estimate_prompt_tokens(growing)
        assert current >= previous
        previous = current


def test_all_callers_share_one_function_object() -> None:
    assert text_tokens.estimate_tokens is spans.estimate_tokens
    assert spans.estimate_tokens is retrieval.estimate_tokens
    assert retrieval.estimate_tokens is knowledge_manager.estimate_tokens
