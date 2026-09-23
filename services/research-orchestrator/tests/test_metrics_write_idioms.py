"""The static metrics preflight must recognize the JSON-write idioms the
temperature-0 workload model actually emits.

Live evidence (run ``afba8d630c7e4dbba553ba200ee050f7``, paused 2026-09-19):
``research-workspace/task-d777d26b32557f22/run.py`` writes metrics with

    metrics_path = output_dir / 'metrics.json'
    metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True) + '\\n')
    assert metrics_path.exists(), 'metrics.json must be written'

The scanner only accepted ``with open('...metrics.json...') as handle:
json.dump(<dict>, handle)``, so it reported ``run.py does not have a statically
verifiable JSON write to metrics.json``. At ``temperature: 0`` the model could
not invent the accepted shape, the identical rejection repeated across
revisions, and the run paused for a human.

These tests pin the corrected rule: a write is verifiable when the *target*
resolves to ``metrics.json`` and the payload is a ``json.dumps`` of a mapping
whose root keys satisfy the contract. The check's substance is unchanged - a
write to another filename, a mapping missing a required root, or no write at
all must still fail.
"""

from __future__ import annotations

import ast

from app.preflight import _metrics_root_errors


# ``schemas/output_schema.json`` of titanic-survival-methodology-v1@1.0.0.
REQUIRED_METRIC_KEYS = [
    'cv_accuracy_mean',
    'cv_roc_auc_mean',
    'cv_f1_macro_mean',
]

_NO_WRITE = (
    'run.py does not have a statically verifiable JSON write to metrics.json'
)


def _errors(source: str) -> list[str]:
    return _metrics_root_errors(
        ast.parse(source),
        relative='run.py',
        required_metric_keys=list(REQUIRED_METRIC_KEYS),
    )


def test_write_text_json_dumps_path_is_a_verifiable_metrics_write() -> None:
    # Given the live run.py's idiom - a Path receiver bound to metrics.json and
    # a json.dumps payload - the scan must accept it.
    source = (
        'import json\n'
        'from pathlib import Path\n'
        'metrics = {\n'
        "    'cv_accuracy_mean': 0.8,\n"
        "    'cv_roc_auc_mean': 0.85,\n"
        "    'cv_f1_macro_mean': 0.77,\n"
        "    'models': {'logistic_regression': {'cv_accuracy_mean': 0.8}},\n"
        '}\n'
        "metrics_path = Path('output') / 'metrics.json'\n"
        "metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True) + '\\n')\n"
        "assert metrics_path.exists(), 'metrics.json must be written'\n"
    )

    assert _errors(source) == []


def test_write_text_json_dumps_inline_path_is_a_verifiable_metrics_write() -> None:
    # The receiver does not have to be a named variable: an inline
    # ``(Path('out') / 'metrics.json')`` expression resolves the same way.
    source = (
        'import json\n'
        'from pathlib import Path\n'
        'metrics = {\n'
        "    'cv_accuracy_mean': 0.8,\n"
        "    'cv_roc_auc_mean': 0.85,\n"
        "    'cv_f1_macro_mean': 0.77,\n"
        '}\n'
        "metrics_path = Path('output') / 'metrics.json'\n"
        'metrics_path.write_text(json.dumps(metrics))\n'
    )

    assert _errors(source) == []


def test_path_open_write_handle_receiving_json_dump_is_verifiable() -> None:
    # ``Path.open('w')`` bound to a handle and then dumped into is the same
    # write as the ``with open(...)`` form, just without the context manager.
    source = (
        'import json\n'
        'from pathlib import Path\n'
        'metrics = {\n'
        "    'cv_accuracy_mean': 0.8,\n"
        "    'cv_roc_auc_mean': 0.85,\n"
        "    'cv_f1_macro_mean': 0.77,\n"
        '}\n'
        "handle = (Path('output') / 'metrics.json').open('w')\n"
        'json.dump(metrics, handle)\n'
        'handle.close()\n'
    )

    assert _errors(source) == []


def test_builtin_open_write_handle_receiving_json_dump_is_verifiable() -> None:
    source = (
        'import json\n'
        'metrics = {\n'
        "    'cv_accuracy_mean': 0.8,\n"
        "    'cv_roc_auc_mean': 0.85,\n"
        "    'cv_f1_macro_mean': 0.77,\n"
        '}\n'
        "handle = open('metrics.json', 'w')\n"
        'json.dump(metrics, handle)\n'
        'handle.close()\n'
    )

    assert _errors(source) == []


def test_with_open_metrics_handle_still_verifiable() -> None:
    # Regression: the previously accepted context-manager idiom must keep
    # working.
    source = (
        'import json\n'
        'metrics = {\n'
        "    'cv_accuracy_mean': 0.8,\n"
        "    'cv_roc_auc_mean': 0.85,\n"
        "    'cv_f1_macro_mean': 0.77,\n"
        '}\n'
        "with open('metrics.json', 'w') as handle:\n"
        '    json.dump(metrics, handle)\n'
    )

    assert _errors(source) == []


def test_write_text_to_non_metrics_path_still_fails() -> None:
    # The check must still require the write target to be metrics.json.
    source = (
        'import json\n'
        'from pathlib import Path\n'
        'metrics = {\n'
        "    'cv_accuracy_mean': 0.8,\n"
        "    'cv_roc_auc_mean': 0.85,\n"
        "    'cv_f1_macro_mean': 0.77,\n"
        '}\n'
        "other_path = Path('output') / 'other.json'\n"
        'other_path.write_text(json.dumps(metrics))\n'
    )

    assert _errors(source) == [_NO_WRITE]


def test_write_text_missing_required_root_key_still_fails() -> None:
    # A verifiable metrics.json write whose mapping omits a contract root must
    # still fail.
    source = (
        'import json\n'
        'from pathlib import Path\n'
        "metrics = {'cv_accuracy_mean': 0.8}\n"
        "metrics_path = Path('output') / 'metrics.json'\n"
        'metrics_path.write_text(json.dumps(metrics))\n'
    )

    assert _errors(source) == [
        'run.py serializes metrics.json without required root key(s): '
        'cv_f1_macro_mean, cv_roc_auc_mean'
    ]


def test_no_metrics_write_at_all_still_fails() -> None:
    source = (
        'import json\n'
        'metrics = {\n'
        "    'cv_accuracy_mean': 0.8,\n"
        "    'cv_roc_auc_mean': 0.85,\n"
        "    'cv_f1_macro_mean': 0.77,\n"
        '}\n'
    )

    assert _errors(source) == [_NO_WRITE]


def test_write_text_of_a_non_json_payload_still_fails() -> None:
    # ``write_text`` alone is not a JSON serialization; only a json.dumps
    # payload counts.
    source = (
        'from pathlib import Path\n'
        "metrics_path = Path('output') / 'metrics.json'\n"
        "metrics_path.write_text('not json')\n"
    )

    assert _errors(source) == [_NO_WRITE]
