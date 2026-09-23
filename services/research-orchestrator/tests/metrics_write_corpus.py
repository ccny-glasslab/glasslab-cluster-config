"""Mechanical corpus of ``metrics.json``-writing ``run.py`` shapes.

The preflight metrics checker (:func:`app.preflight._metrics_root_errors`) is a
static AST scan: it recognizes a JSON write to ``metrics.json`` when the write
*target* resolves to that filename and the payload is a ``json.dumps`` /
``json.dump`` of a mapping whose root keys satisfy the contract. Real models
emit many source shapes that are semantically identical - the same JSON object,
written through a different idiom - so an idiom-enumerating checker produces
false rejections whenever a valid shape is not yet enumerated. That class
blocked run ``afba8d63`` for hours (see ``test_metrics_write_idioms.py``).

This module is the *corpus* that keeps the checker honest:

* ``real/`` holds verbatim excerpts extracted from the frozen live evidence
  corpus on the read-only PVC (``evidence-freeze/20260923``); each file's
  provenance is recorded in its header. These pin idioms models actually emit.
* :func:`generated_variants` mechanically composes the same canonical metrics
  object into many equivalent source shapes (positive cases) and into
  deliberately broken shapes (negative cases: wrong filename, missing root key,
  non-JSON payload, no write).

``tests/test_metrics_write_corpus.py`` runs every variant through the checker
and asserts the outcome. A valid variant that the checker rejects is a real
false negative; a broken variant it accepts would be a false positive.

The generator is deterministic. Run ``python3 tests/metrics_write_corpus.py``
to materialize the ``generated/`` files; ``test_generated_fixtures_match_generator``
fails if the committed files drift from this table.
"""

# allow: SIZE_OK - corpus variant table; the bulk is literal run.py source
# fragments (data), not branching logic.
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Expectation = Literal['accept', 'no_write', 'missing_keys']

FIXTURE_ROOT = Path(__file__).parent / 'fixtures' / 'metrics_write_corpus'
REAL_DIR = FIXTURE_ROOT / 'real'
GENERATED_DIR = FIXTURE_ROOT / 'generated'

# ``schemas/output_schema.json`` of titanic-survival-methodology-v1@1.0.0, the
# same contract roots the PR #529 test pins.
REQUIRED_METRIC_KEYS = (
    'cv_accuracy_mean',
    'cv_roc_auc_mean',
    'cv_f1_macro_mean',
)

_HEADER = "import json\nimport os\nfrom pathlib import Path\n"

_METRICS = (
    "metrics = {\n"
    "    'cv_accuracy_mean': 0.8,\n"
    "    'cv_roc_auc_mean': 0.85,\n"
    "    'cv_f1_macro_mean': 0.77,\n"
    "}\n"
)

_METRICS_PARTIAL = "metrics = {'cv_accuracy_mean': 0.8}\n"

# The two roots omitted by ``_METRICS_PARTIAL``, sorted as the checker reports.
_PARTIAL_MISSING = ('cv_f1_macro_mean', 'cv_roc_auc_mean')


@dataclass(frozen=True, slots=True)
class Variant:
    """One ``run.py`` shape plus the checker outcome it must produce."""

    name: str
    source: str
    expectation: Expectation
    category: str
    required_keys: tuple[str, ...] = REQUIRED_METRIC_KEYS
    missing_keys: tuple[str, ...] = field(default=())


def _accept(name: str, category: str, *body: str) -> Variant:
    return Variant(
        name=name,
        source=_HEADER + ''.join(body),
        expectation='accept',
        category=category,
    )


def _reject(
    name: str,
    category: str,
    expectation: Expectation,
    *body: str,
    missing_keys: tuple[str, ...] = (),
) -> Variant:
    return Variant(
        name=name,
        source=_HEADER + ''.join(body),
        expectation=expectation,
        category=category,
        missing_keys=missing_keys,
    )


def generated_variants() -> list[Variant]:
    """Compose the equivalent and broken shapes from the canonical object."""
    metrics = _METRICS
    partial = _METRICS_PARTIAL
    return [
        # --- valid equivalents: the same JSON, many source shapes ---
        _accept(
            'g01_with_open_builtin',
            'handle',
            metrics,
            "with open('metrics.json', 'w') as f:\n"
            "    json.dump(metrics, f, indent=2)\n",
        ),
        _accept(
            'g02_with_open_pathdiv',
            'handle',
            metrics,
            "out = Path('output')\n"
            "with open(out / 'metrics.json', 'w') as f:\n"
            "    json.dump(metrics, f)\n",
        ),
        _accept(
            'g03_with_open_ospathjoin',
            'handle',
            metrics,
            "out = 'output'\n"
            "with open(os.path.join(out, 'metrics.json'), 'w') as f:\n"
            "    json.dump(metrics, f)\n",
        ),
        _accept(
            'g04_handle_assign_open_dump',
            'handle',
            metrics,
            "f = open('metrics.json', 'w')\n"
            "json.dump(metrics, f)\n"
            "f.close()\n",
        ),
        _accept(
            'g05_path_write_text',
            'write_text',
            metrics,
            "path = Path('output') / 'metrics.json'\n"
            "path.write_text(json.dumps(metrics, indent=2, sort_keys=True))\n",
        ),
        _accept(
            'g06_path_write_text_concat_nl',
            'write_text',
            metrics,
            "path = Path('output') / 'metrics.json'\n"
            "path.write_text(json.dumps(metrics, indent=2, sort_keys=True) + '\\n')\n",
        ),
        _accept(
            'g07_path_open_handle_dump',
            'path_open',
            metrics,
            "handle = (Path('output') / 'metrics.json').open('w')\n"
            "json.dump(metrics, handle)\n"
            "handle.close()\n",
        ),
        _accept(
            'g08_with_path_open_dump',
            'path_open',
            metrics,
            "with (Path('output') / 'metrics.json').open('w') as f:\n"
            "    json.dump(metrics, f)\n",
        ),
        _accept(
            'g09_handle_fwrite_dumps',
            'handle_write',
            metrics,
            "with open('metrics.json', 'w') as f:\n"
            "    f.write(json.dumps(metrics, indent=2))\n",
        ),
        _accept(
            'g10_handle_assign_fwrite_dumps',
            'handle_write',
            metrics,
            "f = open('metrics.json', 'w')\n"
            "f.write(json.dumps(metrics))\n"
            "f.close()\n",
        ),
        _accept(
            'g11_handle_fwrite_dumps_concat_nl',
            'handle_write',
            metrics,
            "with open('metrics.json', 'w') as f:\n"
            "    f.write(json.dumps(metrics, indent=2, sort_keys=True) + '\\n')\n",
        ),
        _accept(
            'g12_inline_dump_open_builtin',
            'inline_handle',
            metrics,
            "json.dump(metrics, open('metrics.json', 'w'))\n",
        ),
        _accept(
            'g13_inline_dump_path_open',
            'inline_handle',
            metrics,
            "json.dump(metrics, (Path('output') / 'metrics.json').open('w'))\n",
        ),
        _accept(
            'g14_inline_open_write',
            'inline_handle',
            metrics,
            "open('metrics.json', 'w').write(json.dumps(metrics))\n",
        ),
        _accept(
            'g15_path_inline_open_write',
            'inline_handle',
            metrics,
            "(Path('output') / 'metrics.json').open('w').write(json.dumps(metrics))\n",
        ),
        _accept(
            'g16_dumps_variable_write_text',
            'dumps_variable',
            metrics,
            "payload = json.dumps(metrics, indent=2)\n"
            "path = Path('output') / 'metrics.json'\n"
            "path.write_text(payload)\n",
        ),
        _accept(
            'g17_dumps_variable_handle_write',
            'dumps_variable',
            metrics,
            "payload = json.dumps(metrics)\n"
            "f = open('metrics.json', 'w')\n"
            "f.write(payload)\n"
            "f.close()\n",
        ),
        _accept(
            'g18_atomic_tempfile_replace',
            'atomic',
            metrics,
            "out = Path('output')\n"
            "tmp = out / 'metrics.json.tmp'\n"
            "tmp.write_text(json.dumps(metrics, indent=2))\n"
            "os.replace(tmp, out / 'metrics.json')\n",
        ),
        _accept(
            'g19_nested_pathdiv',
            'write_text',
            metrics,
            "out = Path('outputs') / 'run-1'\n"
            "out.mkdir(parents=True, exist_ok=True)\n"
            "path = out / 'metrics.json'\n"
            "path.write_text(json.dumps(metrics))\n",
        ),
        _accept(
            'g20_os_path_join_variable',
            'handle',
            metrics,
            "out = 'output'\n"
            "path = os.path.join(out, 'metrics.json')\n"
            "with open(path, 'w') as f:\n"
            "    json.dump(metrics, f)\n",
        ),
        _accept(
            'g21_atomic_os_rename',
            'atomic',
            metrics,
            "out = Path('output')\n"
            "tmp = out / 'metrics.json.tmp'\n"
            "tmp.write_text(json.dumps(metrics))\n"
            "os.rename(tmp, out / 'metrics.json')\n",
        ),
        _accept(
            'g22_atomic_handle_rename',
            'atomic',
            metrics,
            "out = Path('output')\n"
            "tmp = out / 'metrics.json.tmp'\n"
            "with open(tmp, 'w') as f:\n"
            "    json.dump(metrics, f)\n"
            "os.replace(tmp, out / 'metrics.json')\n",
        ),
        _accept(
            'g23_atomic_path_replace',
            'atomic',
            metrics,
            "out = Path('output')\n"
            "tmp = out / 'metrics.json.tmp'\n"
            "tmp.write_text(json.dumps(metrics))\n"
            "tmp.replace(out / 'metrics.json')\n",
        ),
        # --- broken shapes: these must stay rejected ---
        _reject(
            'r01_write_text_wrong_filename',
            'wrong_file',
            'no_write',
            metrics,
            "path = Path('output') / 'other.json'\n"
            "path.write_text(json.dumps(metrics))\n",
        ),
        _reject(
            'r02_handle_wrong_filename',
            'wrong_file',
            'no_write',
            metrics,
            "with open('results.json', 'w') as f:\n"
            "    json.dump(metrics, f)\n",
        ),
        _reject(
            'r03_inline_write_wrong_filename',
            'wrong_file',
            'no_write',
            metrics,
            "open('other.json', 'w').write(json.dumps(metrics))\n",
        ),
        _reject(
            'r04_dump_wrong_file_handle',
            'wrong_file',
            'no_write',
            metrics,
            "f = open('other.json', 'w')\n"
            "json.dump(metrics, f)\n"
            "f.close()\n",
        ),
        _reject(
            'r14_wrong_filename_substring',
            'wrong_file',
            'no_write',
            metrics,
            "open('not_metrics.json', 'w').write(json.dumps(metrics))\n",
        ),
        _reject(
            'r15_wrong_filename_suffix',
            'wrong_file',
            'no_write',
            metrics,
            "path = Path('output') / 'metrics.json.bak'\n"
            "path.write_text(json.dumps(metrics))\n",
        ),
        _reject(
            'r16_atomic_wrong_destination',
            'wrong_file',
            'no_write',
            metrics,
            "out = Path('output')\n"
            "tmp = out / 'scratch.tmp'\n"
            "tmp.write_text(json.dumps(metrics))\n"
            "os.replace(tmp, out / 'other.json')\n",
        ),
        _reject(
            'r05_missing_key_write_text',
            'missing_key',
            'missing_keys',
            partial,
            "path = Path('output') / 'metrics.json'\n"
            "path.write_text(json.dumps(metrics))\n",
            missing_keys=_PARTIAL_MISSING,
        ),
        _reject(
            'r06_missing_key_handle',
            'missing_key',
            'missing_keys',
            partial,
            "with open('metrics.json', 'w') as f:\n"
            "    json.dump(metrics, f)\n",
            missing_keys=_PARTIAL_MISSING,
        ),
        _reject(
            'r07_missing_key_inline_write',
            'missing_key',
            'missing_keys',
            partial,
            "open('metrics.json', 'w').write(json.dumps(metrics))\n",
            missing_keys=_PARTIAL_MISSING,
        ),
        _reject(
            'r08_nonjson_write_text_literal',
            'non_json',
            'no_write',
            "path = Path('output') / 'metrics.json'\n"
            "path.write_text('not json')\n",
        ),
        _reject(
            'r09_nonjson_handle_write_literal',
            'non_json',
            'no_write',
            metrics,
            "with open('metrics.json', 'w') as f:\n"
            "    f.write('not json')\n",
        ),
        _reject(
            'r10_nonjson_str_of_dict',
            'non_json',
            'no_write',
            metrics,
            "with open('metrics.json', 'w') as f:\n"
            "    f.write(str(metrics))\n",
        ),
        _reject(
            'r11_nonjson_variable_write_text',
            'non_json',
            'no_write',
            metrics,
            "report = 'a markdown report'\n"
            "path = Path('output') / 'metrics.json'\n"
            "path.write_text(report)\n",
        ),
        _reject(
            'r12_dumps_only_no_write',
            'no_write',
            'no_write',
            metrics,
            "payload = json.dumps(metrics)\n",
        ),
        _reject(
            'r13_no_write_at_all',
            'no_write',
            'no_write',
            metrics,
        ),
    ]


# Root keys each real excerpt writes, verified against the frozen source.
_REAL_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    'afba8d63_write_text.py': (
        'best_metric',
        'best_model',
        'cv_folds',
        'metric_name',
        'models',
        'seed',
    ),
    'adult_income_ospath_handle.py': (
        'accuracy',
        'f1',
        'precision',
        'recall',
        'roc_auc',
    ),
    'wine_open_pathdiv_handle.py': (
        'accuracy_mean_5cv',
        'accuracy_std_5cv',
        'f1_macro_mean_5cv',
        'f1_macro_std_5cv',
        'fold_metrics',
        'leakage_gap',
        'overfitting_flag',
        'roc_auc_mean_5cv',
        'roc_auc_std_5cv',
        'test_accuracy',
        'train_accuracy',
    ),
    'open_path_variable_handle.py': (
        'cv_results',
        'feature_importance',
        'seed',
        'test_metrics',
    ),
    'write_text_concat_inline.py': (
        'accuracy',
        'balanced_accuracy',
        'bootstrap_resamples',
        'f1',
        'headline_ci_high',
        'headline_ci_low',
        'precision',
        'recall',
        'roc_auc',
        'test_rows',
    ),
    'bare_filename_handle.py': ('cv_folds', 'models', 'seed'),
}


def real_variants() -> list[Variant]:
    """Load the verbatim frozen-corpus excerpts from ``real/``."""
    variants: list[Variant] = []
    for path in sorted(REAL_DIR.glob('*.py')):
        variants.append(
            Variant(
                name=path.stem,
                source=path.read_text(encoding='utf-8'),
                expectation='accept',
                category='real',
                required_keys=_REAL_REQUIRED_KEYS[path.name],
            )
        )
    return variants


def all_variants() -> list[Variant]:
    return real_variants() + generated_variants()


def _write_generated_files() -> None:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    for variant in generated_variants():
        (GENERATED_DIR / f'{variant.name}.py').write_text(
            variant.source,
            encoding='utf-8',
        )


if __name__ == '__main__':
    _write_generated_files()
    print(f'wrote {len(generated_variants())} variants to {GENERATED_DIR}')
