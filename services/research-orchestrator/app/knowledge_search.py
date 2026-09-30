"""Shared lexical-search token filtering for the knowledge stores.

Both research stores (Postgres ``tsvector`` and SQLite FTS5) match knowledge
chunks on *any* query term: agent-context queries concatenate turn kind,
objective, and prompt prefixes into long strings, and AND-ing every token
against short chunks returns nothing. OR-ing every token has the opposite
problem — the token stream is dominated by English function words and
question/boilerplate words, so ``ts_rank_cd``/``bm25`` reward chunks that
merely contain "the", "and", "from" and drown the distinctive terms.

``search_terms`` filters that noise before either store builds its OR query:
tokens that are English stopwords, question words, or prompt boilerplate are
dropped, keeping only terms that carry lexical signal. The filter is
deliberately conservative — domain-ambiguous words (``output``, ``contract``,
``phase``, ``run``, ``model``) are NOT in the set — and any query that
filters down to nothing falls back to the raw token stream so a search never
degenerates to zero candidates.
"""

from __future__ import annotations

from collections.abc import Iterable
import re

# English function words, question words, and high-frequency prompt
# boilerplate. Kept deliberately narrow: domain-ambiguous research terms
# (output, contract, phase, object, run, task, model, ...) are NOT listed.
STOPWORDS: frozenset[str] = frozenset(
    '''
    a an and are as at be been being but by can could did do does doing done
    for from had has have having he her hers him his how i if in into is it its
    me might may more most must my no nor not of off on or our ours out over
    shall she should so some such than that the their theirs them then there
    these they this those through to too under until up upon us very was we were
    what when where which while who whom why will with would you your yours
    about above after again against all also any because before below between
    both during each every few further here just much many neither once only
    other own same several since still well yet
    please note see seen show shows shown given give gave need needs required
    based following above below across along around
    explain tell describe compare contrast versus difference differences
    among list summarize outline detail discuss evaluate assess
    provide answer answers asked asking ask question questions
    complete requested return draft review revise revision approve approval
    '''.split()
)

_ALNUM = re.compile(r'[A-Za-z0-9]')

# Edge punctuation a whitespace-split token picks up from prose: the ``?`` in
# "questions?", the quotes around "term", the comma after "word,". Only the
# boundaries are trimmed, so the hyphen in "state-of-the-art" survives; ``+``
# is kept because it is part of real terms such as "C++".
_EDGE_PUNCTUATION = re.compile(r'^[^A-Za-z0-9+]+|[^A-Za-z0-9+]+$')


def _normalize_token(token: str) -> str:
    """Strip leading/trailing punctuation, preserving internal symbols."""
    return _EDGE_PUNCTUATION.sub('', token)


def _normalized_tokens(query: str) -> list[str]:
    """Whitespace-split tokens with edge punctuation stripped.

    Empty results (tokens that were pure punctuation) are dropped so the
    length and stopword checks below see the same normalized form the store
    later quotes into its MATCH query.
    """
    return [token for token in map(_normalize_token, query.split()) if token]


def _dedupe(tokens: Iterable[str], max_terms: int | None) -> list[str]:
    """Deduplicate by lowercased form, preserving first-seen order."""
    terms: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        lowered = token.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        terms.append(token)
        if max_terms is not None and len(terms) >= max_terms:
            break
    return terms


def filter_search_terms(
    query: str, *, max_terms: int | None = None
) -> list[str]:
    """Distinctive, deduplicated terms only; may be empty.

    Like :func:`search_terms`, but never falls back to the raw token stream.
    Callers that need to distinguish "the query had signal" from "every token
    was noise" (corpus-RAG's AND-first lexical search) use this and supply
    their own behaviour for the empty case.
    """
    tokens = _normalized_tokens(query)
    filtered = [
        token for token in tokens
        if len(token) > 2
        and _ALNUM.search(token)
        and token.lower() not in STOPWORDS
    ]
    return _dedupe(filtered, max_terms)


def search_terms(query: str, *, max_terms: int | None = None) -> list[str]:
    """Split a search query into distinctive, deduplicated terms.

    Drops tokens that are stopwords or that carry no alphanumeric content,
    then deduplicates while preserving first-seen order. If every token is
    filtered away the raw whitespace split (length > 1) is returned so the
    caller still has a query to run.
    """
    terms = filter_search_terms(query, max_terms=max_terms)
    if terms:
        return terms
    return _dedupe(
        [token for token in _normalized_tokens(query) if len(token) > 1],
        max_terms,
    )


def or_query(query: str, *, max_terms: int | None = None) -> str:
    """Build the OR-joined term string shared by both stores."""
    terms = search_terms(query, max_terms=max_terms)
    return ' OR '.join(terms) or query


# The corpus-RAG lexical channel (``search_rag_chunks_fts`` on both stores)
# caps its term list to keep generated tsquery/FTS5 strings bounded. Both
# stores derive their terms here so their matching and ranking stay aligned.
RAG_SEARCH_MAX_TERMS = 24


def rag_significant_terms(query: str) -> list[str]:
    """Distinctive corpus-RAG query terms; empty when every token is noise."""
    return filter_search_terms(query, max_terms=RAG_SEARCH_MAX_TERMS)


def rag_legacy_terms(query: str) -> list[str]:
    """The pre-stopword-filter token stream corpus-RAG falls back to.

    Terms longer than one character, deduplicated and capped, matching the
    behaviour ``search_rag_chunks_fts`` had before stopword filtering. Used
    only when the query has no significant terms at all, so a query like
    "what is it" still searches instead of returning nothing.
    """
    return _dedupe(
        [token for token in _normalized_tokens(query) if len(token) > 1],
        RAG_SEARCH_MAX_TERMS,
    )
