"""Unit tests for the shared knowledge-search token filter."""

from app.knowledge_search import search_terms, or_query, STOPWORDS


def test_search_terms_drops_stopwords_and_boilerplate() -> None:
    terms = search_terms('the from what is conformal prediction and how does it work')
    assert 'conformal' in terms
    assert 'prediction' in terms
    assert 'the' not in terms
    assert 'from' not in terms
    assert 'what' not in terms
    assert 'is' not in terms
    assert 'and' not in terms


def test_search_terms_keeps_domain_words_even_when_common() -> None:
    assert 'model' in search_terms('model output contract phase run task')
    assert 'output' in search_terms('model output contract phase run task')


def test_search_terms_deduplicates_preserving_order() -> None:
    terms = search_terms('conformal conformal prediction prediction coverage')
    assert terms == ['conformal', 'prediction', 'coverage']


def test_search_terms_filters_no_letter_tokens() -> None:
    terms = search_terms('metric learning 2025 ... ---')
    assert '2025' in terms
    assert '...' not in terms
    assert '---' not in terms


def test_search_terms_falls_back_when_everything_is_stopwords() -> None:
    assert search_terms('the and from of to') != []


def test_or_query_joins_filtered_terms() -> None:
    assert or_query('what is the difference between metric and learning') == 'metric OR learning'
    assert or_query('the the the') == 'the'


def test_stopwords_are_lowercase_and_unique() -> None:
    assert all(word == word.lower() for word in STOPWORDS)
    assert len(STOPWORDS) == len(set(STOPWORDS))