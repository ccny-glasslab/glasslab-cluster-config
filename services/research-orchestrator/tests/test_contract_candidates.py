"""Contract candidate sealing, integrity verification, and promotion.

Covers the full seal -> promote -> resolve lifecycle, digest-based tamper
rejection, and the unsupported-input rules (no checksums, no symlinks) that
keep a sealed bundle byte-exact and safe to promote to the trusted catalog.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.contract_candidates import (
    ContractCandidateError,
    ContractCandidateManager,
)
from app.contracts import (
    ContractIntegrityError,
    EvaluationContractResolver,
    compute_contract_digest,
)


def _write_candidate(root: Path) -> None:
    # A complete candidate bundle mirroring what Beaker's agent would produce:
    # descriptor plus wrapper, evaluator, and both JSON schemas.
    root.mkdir(parents=True)
    descriptor = {
        'contract_id': 'candidate-v1',
        'version': '1.0.0',
        'manifest': {
            'primary_metric': 'score',
            'primary_metric_direction': 'maximize',
        },
        'execution_wrapper': 'run_contract.py',
        'evaluation_entry_point': 'evaluator.py',
        'expected_input_schema': 'input.schema.json',
        'expected_output_schema': 'output.schema.json',
        'required_artifacts': ['metrics.json', 'evaluation.json'],
        'resource_constraints': {
            'cpu': 1,
            'memory_gib': 1,
            'gpus': 0,
            'wallclock_minutes': 5,
        },
        'container_image_digest': None,
    }
    (root / 'contract.json').write_text(json.dumps(descriptor))
    (root / 'run_contract.py').write_text('print("wrapper")\n')
    (root / 'evaluator.py').write_text('print("evaluate")\n')
    (root / 'input.schema.json').write_text(
        json.dumps({'type': 'object'})
    )
    (root / 'output.schema.json').write_text(
        json.dumps({'type': 'object'})
    )


def test_candidate_is_sealed_verified_and_promoted(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    _write_candidate(source)
    manager = ContractCandidateManager(
        sealed_root=str(tmp_path / 'sealed'),
        promoted_root=str(tmp_path / 'shared' / 'bundles'),
        catalog_path=str(tmp_path / 'shared' / 'catalog.json'),
        shared_mount_root=str(tmp_path),
    )

    sealed = manager.seal(
        source=source,
        contract_id='candidate-v1',
        version='1.0.0',
    )
    promoted = manager.promote(
        sealed_path=sealed.sealed_path,
        expected_digest=sealed.digest,
    )

    resolved = EvaluationContractResolver(
        str(tmp_path / 'shared' / 'bundles')
    ).resolve('candidate-v1', '1.0.0')
    assert promoted == Path(resolved.root_path)
    assert resolved.digest == sealed.digest
    catalog = json.loads(
        (tmp_path / 'shared' / 'catalog.json').read_text()
    )
    assert catalog['candidate-v1@1.0.0']['digest'] == sealed.digest


def test_sealed_candidate_tampering_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    _write_candidate(source)
    manager = ContractCandidateManager(
        sealed_root=str(tmp_path / 'sealed'),
        promoted_root=str(tmp_path / 'shared' / 'bundles'),
        catalog_path=str(tmp_path / 'shared' / 'catalog.json'),
        shared_mount_root=str(tmp_path),
    )
    sealed = manager.seal(
        source=source,
        contract_id='candidate-v1',
        version='1.0.0',
    )
    evaluator = sealed.sealed_path / 'evaluator.py'
    evaluator.chmod(0o644)
    evaluator.write_text('print("replaced")\n')

    with pytest.raises(ContractIntegrityError, match='digest mismatch'):
        manager.promote(
            sealed_path=sealed.sealed_path,
            expected_digest=sealed.digest,
        )


def test_candidate_cannot_supply_checksum_or_symlink(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    _write_candidate(source)
    (source / 'contract.sha256').write_text('0' * 64)
    manager = ContractCandidateManager(
        sealed_root=str(tmp_path / 'sealed'),
        promoted_root=str(tmp_path / 'shared' / 'bundles'),
        catalog_path=str(tmp_path / 'shared' / 'catalog.json'),
        shared_mount_root=str(tmp_path),
    )
    with pytest.raises(ContractCandidateError, match='unsupported'):
        manager.seal(
            source=source,
            contract_id='candidate-v1',
            version='1.0.0',
        )

    (source / 'contract.sha256').unlink()
    (source / 'linked.py').symlink_to(source / 'evaluator.py')
    with pytest.raises(ContractCandidateError, match='symlinks'):
        manager.seal(
            source=source,
            contract_id='candidate-v1',
            version='1.0.0',
        )


def test_python_bytecode_caches_are_skipped_during_sealing(
    tmp_path: Path,
) -> None:
    # Beaker runs its local checks inside the candidate directory, and
    # CPython leaves __pycache__/*.pyc behind. Those are reproducible
    # interpreter byproducts rather than reviewed content, and the
    # review-copy path already ignores them; sealing must skip them too.
    # Issue #98 run 6ba79481df7142a89ee67050b0fb37e4 exhausted its turn
    # budget on exactly this rejection.
    source = tmp_path / 'source'
    _write_candidate(source)
    pycache = source / 'src' / '__pycache__'
    pycache.mkdir(parents=True)
    (pycache / 'evaluator.cpython-311.pyc').write_bytes(b'\x00\x01cache')
    manager = ContractCandidateManager(
        sealed_root=str(tmp_path / 'sealed'),
        promoted_root=str(tmp_path / 'shared' / 'bundles'),
        catalog_path=str(tmp_path / 'shared' / 'catalog.json'),
        shared_mount_root=str(tmp_path),
    )

    sealed = manager.seal(
        source=source,
        contract_id='candidate-v1',
        version='1.0.0',
    )

    assert not list(sealed.sealed_path.rglob('*.pyc'))
    assert not list(sealed.sealed_path.rglob('__pycache__'))
    assert (sealed.sealed_path / 'evaluator.py').is_file()


def test_unknown_non_text_content_is_still_rejected(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    _write_candidate(source)
    (source / 'helper.sh').write_text('#!/bin/sh\n')
    manager = ContractCandidateManager(
        sealed_root=str(tmp_path / 'sealed'),
        promoted_root=str(tmp_path / 'shared' / 'bundles'),
        catalog_path=str(tmp_path / 'shared' / 'catalog.json'),
        shared_mount_root=str(tmp_path),
    )

    with pytest.raises(ContractCandidateError, match='unsupported'):
        manager.seal(
            source=source,
            contract_id='candidate-v1',
            version='1.0.0',
        )


def test_candidate_methodology_requirements_missing_config_path_is_rejected(
    tmp_path: Path,
) -> None:
    source = tmp_path / 'source'
    _write_candidate(source)
    descriptor_path = source / 'contract.json'
    descriptor = json.loads(descriptor_path.read_text())
    descriptor['manifest']['methodology_requirements'] = [
        {
            'requirement_id': 'calibration-metric-threshold',
            'mode': 'decision',
            'description': 'accuracy is the primary metric',
        }
    ]
    descriptor_path.write_text(json.dumps(descriptor))
    manager = ContractCandidateManager(
        sealed_root=str(tmp_path / 'sealed'),
        promoted_root=str(tmp_path / 'shared' / 'bundles'),
        catalog_path=str(tmp_path / 'shared' / 'catalog.json'),
        shared_mount_root=str(tmp_path),
    )
    with pytest.raises(ContractCandidateError, match='methodology_requirements'):
        manager.seal(
            source=source,
            contract_id='candidate-v1',
            version='1.0.0',
        )


def test_candidate_methodology_requirements_filesystem_config_path_is_rejected(
    tmp_path: Path,
) -> None:
    # Issue #198: config_path is a dotted key path into the matrix.base_config
    # YAML, never a filesystem path. A value like "src/train.py" previously
    # sealed cleanly and then demanded a nonsensical nested key
    # src: {train: {py: [...]}} at preflight, burning six revision cycles.
    source = tmp_path / 'source'
    _write_candidate(source)
    descriptor_path = source / 'contract.json'
    descriptor = json.loads(descriptor_path.read_text())
    descriptor['manifest']['methodology_requirements'] = [
        {
            'requirement_id': 'baseline-comparison',
            'config_path': 'src/train.py',
            'mode': 'comparison',
            'comparison_scope': 'within_job',
            'minimum_distinct_values': 2,
            'description': 'compare baseline with non-linear ensembles',
        }
    ]
    descriptor_path.write_text(json.dumps(descriptor))
    manager = ContractCandidateManager(
        sealed_root=str(tmp_path / 'sealed'),
        promoted_root=str(tmp_path / 'shared' / 'bundles'),
        catalog_path=str(tmp_path / 'shared' / 'catalog.json'),
        shared_mount_root=str(tmp_path),
    )
    with pytest.raises(ContractCandidateError, match='dotted key path'):
        manager.seal(
            source=source,
            contract_id='candidate-v1',
            version='1.0.0',
        )


def test_promotion_rejects_filesystem_config_path_in_sealed_descriptor(
    tmp_path: Path,
) -> None:
    # A sealed bundle is re-validated at promotion, so a contract sealed before
    # this check existed cannot slip a filesystem-looking config_path into the
    # trusted catalog (issue #198).
    source = tmp_path / 'source'
    _write_candidate(source)
    manager = ContractCandidateManager(
        sealed_root=str(tmp_path / 'sealed'),
        promoted_root=str(tmp_path / 'shared' / 'bundles'),
        catalog_path=str(tmp_path / 'shared' / 'catalog.json'),
        shared_mount_root=str(tmp_path),
    )
    sealed = manager.seal(
        source=source,
        contract_id='candidate-v1',
        version='1.0.0',
    )
    descriptor_path = sealed.sealed_path / 'contract.json'
    checksum_path = sealed.sealed_path / 'contract.sha256'
    descriptor_path.chmod(0o644)
    checksum_path.chmod(0o644)
    descriptor = json.loads(descriptor_path.read_text())
    descriptor['manifest']['methodology_requirements'] = [
        {
            'requirement_id': 'baseline-comparison',
            'config_path': 'configs/train.yaml',
            'mode': 'comparison',
            'comparison_scope': 'within_job',
            'minimum_distinct_values': 2,
            'description': 'compare baseline with non-linear ensembles',
        }
    ]
    descriptor_path.write_text(json.dumps(descriptor))
    digest = compute_contract_digest(sealed.sealed_path)
    checksum_path.write_text(digest + '\n')

    with pytest.raises(ContractCandidateError, match='dotted key path'):
        manager.promote(
            sealed_path=sealed.sealed_path,
            expected_digest=digest,
        )


def test_candidate_valid_methodology_requirements_seal_cleanly(
    tmp_path: Path,
) -> None:
    source = tmp_path / 'source'
    _write_candidate(source)
    descriptor_path = source / 'contract.json'
    descriptor = json.loads(descriptor_path.read_text())
    descriptor['manifest']['methodology_requirements'] = [
        {
            'requirement_id': 'calibration-metric-threshold',
            'config_path': 'experiment_dimensions.model',
            'mode': 'decision',
            'description': 'accuracy is the primary metric',
        }
    ]
    descriptor_path.write_text(json.dumps(descriptor))
    manager = ContractCandidateManager(
        sealed_root=str(tmp_path / 'sealed'),
        promoted_root=str(tmp_path / 'shared' / 'bundles'),
        catalog_path=str(tmp_path / 'shared' / 'catalog.json'),
        shared_mount_root=str(tmp_path),
    )
    sealed = manager.seal(
        source=source,
        contract_id='candidate-v1',
        version='1.0.0',
    )
    assert sealed.digest


def _manager(tmp_path: Path) -> ContractCandidateManager:
    return ContractCandidateManager(
        sealed_root=str(tmp_path / 'sealed'),
        promoted_root=str(tmp_path / 'shared' / 'bundles'),
        catalog_path=str(tmp_path / 'shared' / 'catalog.json'),
        shared_mount_root=str(tmp_path),
    )


def _candidate_with_requirements(
    tmp_path: Path,
    requirements: list[dict[str, object]],
) -> tuple[ContractCandidateManager, Path]:
    # Reuse the complete candidate bundle and swap in the methodology
    # requirements under test, mirroring what Beaker's agent emits.
    source = tmp_path / 'source'
    _write_candidate(source)
    descriptor_path = source / 'contract.json'
    descriptor = json.loads(descriptor_path.read_text())
    descriptor['manifest']['methodology_requirements'] = requirements
    descriptor_path.write_text(json.dumps(descriptor))
    return _manager(tmp_path), source


def _candidate_with_budget(
    tmp_path: Path,
    budget: object,
) -> tuple[ContractCandidateManager, Path]:
    # manifest.budget is informational, but a declaration that exceeds the
    # contract's own resource_constraints contradicts the artifact a human is
    # asked to approve (issue #500).
    source = tmp_path / 'source'
    _write_candidate(source)
    descriptor_path = source / 'contract.json'
    descriptor = json.loads(descriptor_path.read_text())
    descriptor['manifest']['budget'] = budget
    descriptor_path.write_text(json.dumps(descriptor))
    return _manager(tmp_path), source


def test_candidate_budget_above_resource_constraints_is_rejected(
    tmp_path: Path,
) -> None:
    manager, source = _candidate_with_budget(
        tmp_path,
        {'wallclock_minutes': 600},
    )

    with pytest.raises(ContractCandidateError, match='manifest.budget'):
        _seal(manager, source)


def test_candidate_budget_within_resource_constraints_seals(
    tmp_path: Path,
) -> None:
    manager, source = _candidate_with_budget(
        tmp_path,
        {'wallclock_minutes': 5, 'notes': 'Small deterministic evaluator.'},
    )

    sealed = manager.seal(
        source=source,
        contract_id='candidate-v1',
        version='1.0.0',
    )

    assert sealed.descriptor.manifest['budget'] == {
        'wallclock_minutes': 5,
        'notes': 'Small deterministic evaluator.',
    }


def test_candidate_malformed_budget_is_rejected(tmp_path: Path) -> None:
    manager, source = _candidate_with_budget(tmp_path, 'ten minutes')

    with pytest.raises(ContractCandidateError, match='manifest.budget'):
        _seal(manager, source)


def _seal(manager: ContractCandidateManager, source: Path) -> None:
    manager.seal(source=source, contract_id='candidate-v1', version='1.0.0')


def test_candidate_valid_comparison_requirement_seals_cleanly(
    tmp_path: Path,
) -> None:
    manager, source = _candidate_with_requirements(
        tmp_path,
        [
            {
                'requirement_id': 'model_families',
                'config_path': 'experiment_dimensions.model',
                'mode': 'comparison',
                'comparison_scope': 'within_job',
                'minimum_distinct_values': 2,
                'description': 'Compare a linear and a non-linear model family.',
            }
        ],
    )

    sealed = manager.seal(
        source=source,
        contract_id='candidate-v1',
        version='1.0.0',
    )

    assert sealed.digest


def test_candidate_comparison_requirement_without_scope_is_rejected(
    tmp_path: Path,
) -> None:
    # Ambiguous comparison semantics must never seal: every comparison
    # requirement declares its scope explicitly so a new contract cannot fall
    # back to the legacy within_job behavior by omission.
    manager, source = _candidate_with_requirements(
        tmp_path,
        [
            {
                'requirement_id': 'model_families',
                'config_path': 'experiment_dimensions.model',
                'mode': 'comparison',
                'minimum_distinct_values': 2,
                'description': 'Compare a linear and a non-linear model family.',
            }
        ],
    )

    with pytest.raises(ContractCandidateError, match='comparison_scope'):
        _seal(manager, source)


def test_candidate_decision_requirement_with_scope_is_rejected(
    tmp_path: Path,
) -> None:
    manager, source = _candidate_with_requirements(
        tmp_path,
        [
            {
                'requirement_id': 'missing_data_strategy',
                'config_path': 'experiment_dimensions.missing_strategy',
                'mode': 'decision',
                'comparison_scope': 'within_job',
                'description': 'Choose one missing-data strategy.',
            }
        ],
    )

    with pytest.raises(ContractCandidateError, match='decision requirement'):
        _seal(manager, source)


def test_candidate_multiple_across_jobs_comparisons_are_rejected(
    tmp_path: Path,
) -> None:
    manager, source = _candidate_with_requirements(
        tmp_path,
        [
            {
                'requirement_id': 'model_families',
                'config_path': 'experiment_dimensions.model',
                'mode': 'comparison',
                'comparison_scope': 'across_jobs',
                'minimum_distinct_values': 2,
                'description': 'Compare model families across jobs.',
            },
            {
                'requirement_id': 'search_technique',
                'config_path': 'experiment_dimensions.search_technique',
                'mode': 'comparison',
                'comparison_scope': 'across_jobs',
                'minimum_distinct_values': 2,
                'description': 'Compare search techniques across jobs.',
            },
        ],
    )

    with pytest.raises(
        ContractCandidateError,
        match='more than one across_jobs',
    ):
        _seal(manager, source)


def test_candidate_across_jobs_without_comparison_key_is_rejected(
    tmp_path: Path,
) -> None:
    # An across_jobs evaluator cannot see sibling jobs, so the sealed output
    # schema must declare the comparison_key digest property; otherwise the
    # comparison can never be attested and sealing must fail.
    manager, source = _candidate_with_requirements(
        tmp_path,
        [
            {
                'requirement_id': 'model_families',
                'config_path': 'experiment_dimensions.model',
                'mode': 'comparison',
                'comparison_scope': 'across_jobs',
                'minimum_distinct_values': 2,
                'description': 'Compare model families across jobs.',
            }
        ],
    )
    (source / 'output.schema.json').write_text(
        json.dumps({'type': 'object'})
    )

    with pytest.raises(ContractCandidateError, match='comparison_key'):
        _seal(manager, source)


def test_candidate_across_jobs_with_comparison_key_seals_cleanly(
    tmp_path: Path,
) -> None:
    manager, source = _candidate_with_requirements(
        tmp_path,
        [
            {
                'requirement_id': 'model_families',
                'config_path': 'experiment_dimensions.model',
                'mode': 'comparison',
                'comparison_scope': 'across_jobs',
                'minimum_distinct_values': 2,
                'description': 'Compare model families across jobs.',
            }
        ],
    )
    (source / 'output.schema.json').write_text(
        json.dumps(
            {
                'type': 'object',
                'properties': {'comparison_key': {'type': 'string'}},
            }
        )
    )

    sealed = manager.seal(
        source=source,
        contract_id='candidate-v1',
        version='1.0.0',
    )

    assert sealed.digest


def test_candidate_unknown_root_config_path_is_rejected(tmp_path: Path) -> None:
    # Issue #457: a requirement rooted anywhere other than the
    # experiment_dimensions namespace cannot be materialized by the matrix
    # template, so sealing it would send the run into a revision loop.
    manager, source = _candidate_with_requirements(
        tmp_path,
        [
            {
                'requirement_id': 'search_technique',
                'config_path': 'methodology.search_technique',
                'mode': 'comparison',
                'comparison_scope': 'within_job',
                'minimum_distinct_values': 2,
                'description': 'Compare at least two search techniques.',
            }
        ],
    )

    with pytest.raises(
        ContractCandidateError,
        match='experiment_dimensions',
    ) as excinfo:
        _seal(manager, source)

    assert 'search_technique' in str(excinfo.value)
    assert 'methodology.search_technique' in str(excinfo.value)


def test_candidate_config_path_must_address_a_dimension_key(
    tmp_path: Path,
) -> None:
    # A bare root names the whole experiment_dimensions mapping, which preflight
    # rejects as a metadata object; sealing must require a nested dimension key.
    manager, source = _candidate_with_requirements(
        tmp_path,
        [
            {
                'requirement_id': 'dimensions-root',
                'config_path': 'experiment_dimensions',
                'mode': 'decision',
                'description': 'Pick one experiment dimension.',
            }
        ],
    )

    with pytest.raises(
        ContractCandidateError,
        match='experiment_dimensions.model',
    ):
        _seal(manager, source)


@pytest.mark.parametrize(
    'config_path',
    [
        '../experiment_dimensions/model',
        '/etc/passwd',
        'experiment_dimensions/../model',
        'experiment_dimensions..model',
        'experiment_dimensions.',
        'experiment_dimensions/model',
        'src/train.py',
        'configs/train.yaml',
    ],
)
def test_candidate_malformed_config_path_is_rejected(
    tmp_path: Path,
    config_path: str,
) -> None:
    manager, source = _candidate_with_requirements(
        tmp_path,
        [
            {
                'requirement_id': 'baseline-comparison',
                'config_path': config_path,
                'mode': 'comparison',
                'comparison_scope': 'within_job',
                'minimum_distinct_values': 2,
                'description': 'Compare baseline with non-linear ensembles.',
            }
        ],
    )

    with pytest.raises(ContractCandidateError, match='dotted key path'):
        _seal(manager, source)


def test_candidate_comparison_requirement_needs_two_values(
    tmp_path: Path,
) -> None:
    manager, source = _candidate_with_requirements(
        tmp_path,
        [
            {
                'requirement_id': 'model_families',
                'config_path': 'experiment_dimensions.model',
                'mode': 'comparison',
                'comparison_scope': 'within_job',
                'minimum_distinct_values': 1,
                'description': 'Compare model families.',
            }
        ],
    )

    with pytest.raises(
        ContractCandidateError,
        match='comparison requirement must',
    ) as excinfo:
        _seal(manager, source)

    assert 'model_families' in str(excinfo.value)
    assert 'experiment_dimensions.model' in str(excinfo.value)


def test_candidate_decision_requirement_pins_exactly_one_value(
    tmp_path: Path,
) -> None:
    manager, source = _candidate_with_requirements(
        tmp_path,
        [
            {
                'requirement_id': 'missing_data_strategy',
                'config_path': 'experiment_dimensions.missing_strategy',
                'mode': 'decision',
                'minimum_distinct_values': 2,
                'description': 'Choose one missing-data strategy.',
            }
        ],
    )

    with pytest.raises(
        ContractCandidateError,
        match='decision requirement must',
    ) as excinfo:
        _seal(manager, source)

    assert 'missing_data_strategy' in str(excinfo.value)


def test_candidate_duplicate_requirement_id_is_rejected(tmp_path: Path) -> None:
    manager, source = _candidate_with_requirements(
        tmp_path,
        [
            {
                'requirement_id': 'model_families',
                'config_path': 'experiment_dimensions.model',
                'mode': 'comparison',
                'comparison_scope': 'within_job',
                'minimum_distinct_values': 2,
                'description': 'Compare model families.',
            },
            {
                'requirement_id': 'model_families',
                'config_path': 'experiment_dimensions.encoding',
                'mode': 'decision',
                'description': 'Choose one encoding.',
            },
        ],
    )

    with pytest.raises(
        ContractCandidateError,
        match='duplicate requirement_id',
    ) as excinfo:
        _seal(manager, source)

    assert 'model_families' in str(excinfo.value)


def test_candidate_maximum_below_minimum_is_rejected(tmp_path: Path) -> None:
    manager, source = _candidate_with_requirements(
        tmp_path,
        [
            {
                'requirement_id': 'model_families',
                'config_path': 'experiment_dimensions.model',
                'mode': 'comparison',
                'comparison_scope': 'within_job',
                'minimum_distinct_values': 3,
                'maximum_distinct_values': 2,
                'description': 'Compare model families.',
            }
        ],
    )

    with pytest.raises(
        ContractCandidateError,
        match='below minimum_distinct_values',
    ) as excinfo:
        _seal(manager, source)

    assert 'model_families' in str(excinfo.value)


def test_candidate_blank_requirement_fields_are_rejected(
    tmp_path: Path,
) -> None:
    manager, source = _candidate_with_requirements(
        tmp_path,
        [
            {
                'requirement_id': '   ',
                'config_path': 'experiment_dimensions.model',
                'mode': 'decision',
                'description': '   ',
            }
        ],
    )

    with pytest.raises(ContractCandidateError, match='non-empty'):
        _seal(manager, source)


def test_candidate_empty_requirement_id_is_rejected(tmp_path: Path) -> None:
    manager, source = _candidate_with_requirements(
        tmp_path,
        [
            {
                'requirement_id': '',
                'config_path': 'experiment_dimensions.model',
                'mode': 'decision',
                'description': 'accuracy is the primary metric',
            }
        ],
    )

    with pytest.raises(ContractCandidateError, match='methodology_requirements'):
        _seal(manager, source)


def _promote_fresh_candidate(
    tmp_path: Path,
    manager: ContractCandidateManager,
    *,
    version: str,
    marker: str,
) -> str:
    source = tmp_path / f'source-{marker}'
    _write_candidate(source)
    descriptor_path = source / 'contract.json'
    descriptor = json.loads(descriptor_path.read_text())
    descriptor['version'] = version
    descriptor_path.write_text(json.dumps(descriptor))
    (source / 'evaluator.py').write_text(f'print("evaluate {marker}")\n')
    sealed = manager.seal(
        source=source,
        contract_id='candidate-v1',
        version=version,
    )
    manager.promote(
        sealed_path=sealed.sealed_path,
        expected_digest=sealed.digest,
    )
    return sealed.digest


def test_allocate_version_returns_requested_version_when_free(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)

    version, newly_reserved = manager.allocate_version(
        contract_id='candidate-v1',
        requested_version='1.0.0',
        run_id='run-a',
    )

    assert version == '1.0.0'
    assert newly_reserved is True


def test_allocate_version_bumps_past_a_promoted_version(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    _promote_fresh_candidate(
        tmp_path, manager, version='1.0.0', marker='first'
    )

    version, newly_reserved = manager.allocate_version(
        contract_id='candidate-v1',
        requested_version='1.0.0',
        run_id='run-b',
    )

    assert version == '1.0.1'
    assert newly_reserved is True


def test_allocate_version_is_idempotent_per_run_and_unique_across_runs(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)

    first = manager.allocate_version(
        contract_id='candidate-v1',
        requested_version='1.0.0',
        run_id='run-b',
    )
    again = manager.allocate_version(
        contract_id='candidate-v1',
        requested_version='1.0.0',
        run_id='run-b',
    )
    other = manager.allocate_version(
        contract_id='candidate-v1',
        requested_version='1.0.0',
        run_id='run-c',
    )

    assert first == ('1.0.0', True)
    assert again == ('1.0.0', False)
    assert other == ('1.0.1', True)


def test_allocated_version_promotes_alongside_the_installed_contract(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    first_digest = _promote_fresh_candidate(
        tmp_path, manager, version='1.0.0', marker='first'
    )

    version, _ = manager.allocate_version(
        contract_id='candidate-v1',
        requested_version='1.0.0',
        run_id='run-b',
    )
    second_digest = _promote_fresh_candidate(
        tmp_path, manager, version=version, marker='second'
    )
    assert second_digest != first_digest

    resolver = EvaluationContractResolver(str(tmp_path / 'shared' / 'bundles'))
    assert resolver.resolve('candidate-v1', '1.0.0').digest == first_digest
    assert resolver.resolve('candidate-v1', '1.0.1').digest == second_digest
    catalog = json.loads(
        (tmp_path / 'shared' / 'catalog.json').read_text()
    )
    assert catalog['candidate-v1@1.0.0']['digest'] == first_digest
    assert catalog['candidate-v1@1.0.1']['digest'] == second_digest


def test_promotion_still_refuses_to_overwrite_an_occupied_version(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    _promote_fresh_candidate(
        tmp_path, manager, version='1.0.0', marker='first'
    )
    source = tmp_path / 'source-second'
    _write_candidate(source)
    (source / 'evaluator.py').write_text('print("evaluate second")\n')
    second = manager.seal(
        source=source,
        contract_id='candidate-v1',
        version='1.0.0',
    )

    with pytest.raises(
        ContractCandidateError,
        match='already promoted with another digest',
    ):
        manager.promote(
            sealed_path=second.sealed_path,
            expected_digest=second.digest,
        )
