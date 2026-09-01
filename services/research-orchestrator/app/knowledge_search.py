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


def search_terms(query: str, *, max_terms: int | None = None) -> list[str]:
    """Split a search query into distinctive, deduplicated terms.

    Drops tokens that are stopwords or that carry no alphanumeric content,
    then deduplicates while preserving first-seen order. If every token is
    filtered away the raw whitespace split (length > 1) is returned so the
    caller still has a query to run.
    """
    tokens = query.split()
    filtered = [
        token for token in tokens
        if len(token) > 2
        and _ALNUM.search(token)
        and token.lower() not in STOPWORDS
    ]
    terms: list[str] = []
    seen: set[str] = set()
    for token in filtered or [t for t in tokens if len(t) > 1]:
        lowered = token.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        terms.append(token)
    if max_terms is not None:
        terms = terms[:max_terms]
    return terms


def or_query(query: str, *, max_terms: int | None = None) -> str:
    """Build the OR-joined term string shared by both stores."""
    terms = search_terms(query, max_terms=max_terms)
    return ' OR '.join(terms) or query