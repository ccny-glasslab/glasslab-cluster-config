"""ADR-0005 resource-authority clamp: RED tests (Wave 2 / T5).

The compiled task's runtime profile is the single authoritative source of an
imported job's requested resources
(``docs/glasslab-v2/adr/0005-runtime-profile-resource-authority.md``). Its
decision resolves every resource dimension by::

    effective = min(request, profile.envelope, contract.constraints, policy)

A matrix may copy the profile or request less; it may never exceed the
resolved envelope. ``contract.constraints`` is a compatibility envelope, not
an independent ceiling: it must be a superset of the profile, validated once
at promotion and re-checked at binding, failing closed with an actionable
message.

These tests are written test-first. The ADR's "Current implementation state"
records that the clamp half is unimplemented at this revision, and that for
imported tasks the matrix must currently equal the profile exactly
(``engine.py:5256-5266``). Every test is ``xfail(strict=True)`` until T10
implements the clamp: a strict XPASS means the behavior landed and the marker
must be removed.

The proposed seam under test is ``app.preflight.resolve_effective_resources``
plus its ``ResourceAuthorityError``, so T10 can call one pure function from
seal, promotion, binding, and matrix preflight. The lookups below are
deliberate: a missing symbol must fail as an assertion about absent behavior,
never as a module-level import error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import preflight
from app.discord_adapter import DisabledDiscordAdapter
from app.engine import ResearchOrchestrator
from app.preflight import profile_contract_resource_conflicts
from app.schemas import (
    ResourceRequest,
    RunCreateRequest,
    RunState,
)
from app.task_bundles import RUNTIME_PROFILES

from conftest import RUNNER_IMAGE
from test_resource_authority import (
    ACCOMMODATING_CONSTRAINTS,
    CONTRACT_CONSTRAINTS,
    CPU_PROFILE,
    _ProfileProtocolRuntime,
    _bind_profile_task_and_park_for_promotion,
    _bind_task_profile_and_contract,
    _events,
    _save_approved_promotion_action,
    _save_pending_matrix,
)


# The global policy clamp (ADR-0005 §Decision: policy.py:146-156). Policy has
# no wall-clock dimension, so it is deliberately absent from this mapping.
POLICY = {'cpu': 8, 'memory_gib': 32, 'gpus': 1}


def _resolve_effective_resources():
    resolver = getattr(preflight, 'resolve_effective_resources', None)
    assert resolver is not None, (
        'ADR-0005 §Decision requires app.preflight.resolve_effective_resources: '
        'the per-dimension clamp effective = min(request, profile.envelope, '
        'contract.constraints, policy). The clamp half is unimplemented at '
        'this revision (ADR-0005 "Current implementation state").'
    )
    return resolver


def _resource_authority_error():
    error_type = getattr(preflight, 'ResourceAuthorityError', None)
    assert error_type is not None, (
        'ADR-0005 §Decision requires app.preflight.ResourceAuthorityError: a '
        'contract whose resource_constraints is not a superset of the profile '
        'must fail closed, not silently clamp.'
    )
    return error_type


def _effective(
    resolver,
    *,
    request,
    profile,
    constraints,
    policy,
):
    result = resolver(
        request=request,
        profile=profile,
        constraints=constraints,
        policy=policy,
    )
    return ResourceRequest.model_validate(result)


def _normalized(resources) -> dict[str, float]:
    request = ResourceRequest.model_validate(resources)
    return {
        'cpu': float(request.cpu),
        'memory_gib': float(request.memory_gib),
        'gpus': int(request.gpus),
        'wallclock_minutes': int(request.wallclock_minutes),
    }


# Table-driven: one row per authority that can bind a dimension, plus a mixed
# row. ``profile`` may omit a dimension (undeclared dimensions are uncapped,
# matching ``profile_contract_resource_conflicts``); ``constraints`` must be a
# superset of every declared profile dimension or the resolver fails closed.
CLAMP_CASES = [
    pytest.param(
        {'cpu': 2, 'memory_gib': 4, 'gpus': 0, 'wallclock_minutes': 30},
        {'cpu': 4, 'memory_gib': 8, 'gpus': 0, 'wallclock_minutes': 60},
        {'cpu': 8, 'memory_gib': 32, 'gpus': 1, 'wallclock_minutes': 120},
        dict(POLICY),
        {'cpu': 2.0, 'memory_gib': 4.0, 'gpus': 0, 'wallclock_minutes': 30},
        id='request-binding',
    ),
    pytest.param(
        {'cpu': 16, 'memory_gib': 64, 'gpus': 2, 'wallclock_minutes': 300},
        {'cpu': 4, 'memory_gib': 8, 'gpus': 0, 'wallclock_minutes': 60},
        {'cpu': 8, 'memory_gib': 32, 'gpus': 1, 'wallclock_minutes': 120},
        dict(POLICY),
        {'cpu': 4.0, 'memory_gib': 8.0, 'gpus': 0, 'wallclock_minutes': 60},
        id='profile-envelope-binding',
    ),
    pytest.param(
        {'cpu': 4, 'memory_gib': 8, 'gpus': 0, 'wallclock_minutes': 90},
        {'cpu': 4, 'memory_gib': 8, 'gpus': 0},
        {'cpu': 8, 'memory_gib': 32, 'gpus': 0, 'wallclock_minutes': 30},
        dict(POLICY),
        {'cpu': 4.0, 'memory_gib': 8.0, 'gpus': 0, 'wallclock_minutes': 30},
        id='contract-constraints-binding',
    ),
    pytest.param(
        {'cpu': 8, 'memory_gib': 32, 'gpus': 1, 'wallclock_minutes': 120},
        {'cpu': 8, 'memory_gib': 32, 'gpus': 1, 'wallclock_minutes': 120},
        {'cpu': 8, 'memory_gib': 32, 'gpus': 1, 'wallclock_minutes': 240},
        {'cpu': 2, 'memory_gib': 4, 'gpus': 0},
        {'cpu': 2.0, 'memory_gib': 4.0, 'gpus': 0, 'wallclock_minutes': 120},
        id='policy-binding',
    ),
    pytest.param(
        {'cpu': 16, 'memory_gib': 64, 'gpus': 2, 'wallclock_minutes': 90},
        {'cpu': 8, 'memory_gib': 32, 'gpus': 1},
        {'cpu': 16, 'memory_gib': 64, 'gpus': 1, 'wallclock_minutes': 45},
        {'cpu': 4, 'memory_gib': 8, 'gpus': 1},
        {'cpu': 4.0, 'memory_gib': 8.0, 'gpus': 1, 'wallclock_minutes': 45},
        id='mixed-authorities',
    ),
]


@pytest.mark.xfail(
    strict=True,
    reason='ADR-0005 clamp unimplemented (T10); exact-match at engine.py:5262',
)
@pytest.mark.parametrize(
    ('matrix_request', 'profile', 'constraints', 'policy', 'expected'),
    CLAMP_CASES,
)
def test_effective_resources_is_min_of_all_authorities(
    matrix_request,
    profile,
    constraints,
    policy,
    expected,
) -> None:
    """ADR-0005 §Decision: ``effective = min(request, profile.envelope,
    contract.constraints, policy)`` is evaluated per dimension. The matrix may
    request less than the profile envelope, and every authority clamps.
    """
    resolver = _resolve_effective_resources()
    effective = _effective(
        resolver,
        request=matrix_request,
        profile=profile,
        constraints=constraints,
        policy=policy,
    )
    assert _normalized(effective) == expected


@pytest.mark.xfail(
    strict=True,
    reason='ADR-0005 clamp unimplemented (T10); exact-match at engine.py:5262',
)
def test_matrix_may_request_less_than_profile(
    tmp_path,
    orchestrator_bundle,
    monkeypatch,
) -> None:
    """ADR-0005 §Decision: "A task may not invent resources, but it may
    request less than what the platform envelope allows." A matrix below the
    profile envelope must pass deterministic preflight; the exact-match rule
    at ``engine.py:5262-5266`` is the gap this test pins.
    """
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Accept a matrix below the profile.')
    )
    workspace = Path(run.beaker_workspace)
    (workspace / 'configs').mkdir(parents=True, exist_ok=True)
    (workspace / 'configs' / 'candidate.yaml').write_text(
        'model: [logistic_regression]\n'
    )
    (workspace / 'implementation-plan.md').write_text('# Plan\n')
    source = workspace / 'benchmark-workspace' / 'titanic'
    source.mkdir(parents=True)
    (source / 'run.py').write_text(
        'import json\n'
        'with open("metrics.json", "w") as handle:\n'
        '    json.dump({"score": 1.0}, handle)\n'
    )
    run, _ = _bind_task_profile_and_contract(
        tmp_path,
        store,
        engine,
        run,
        resource_constraints=ACCOMMODATING_CONSTRAINTS,
    )
    current = store.get_run(run.run_id)
    store.replace_run(
        current.model_copy(update={'state': RunState.HONEYDEW_REVIEWING}),
        expected_version=current.version,
    )
    # Strictly below cpu-ml-standard-v1 (4/8/0/60) and inside the contract.
    action = _save_pending_matrix(
        store,
        run_id=run.run_id,
        resources={
            'cpu': 2,
            'memory_gib': 4,
            'gpus': 0,
            'wallclock_minutes': 45,
        },
        ordinal='below-profile',
    )
    revised: list[str] = []
    monkeypatch.setattr(
        engine,
        '_beaker_revise',
        lambda run_id, *, feedback: revised.append(feedback),
    )

    engine._honeydew_review(run.run_id, implementation_turn_id='turn-1')

    reviewed = store.get_run(run.run_id)
    assert reviewed.state == RunState.AWAITING_EXECUTION_APPROVAL
    assert store.get_action(action.action_id).honeydew_approved is True
    assert revised == []
    assert (
        _events(store, run.run_id, 'methodology.resource_authority_conflict')
        == []
    )


@pytest.mark.xfail(
    strict=True,
    reason='ADR-0005 superset seam unimplemented (T10)',
)
def test_contract_constraints_must_superset_profile_at_promotion(
    tmp_path,
    orchestrator_bundle,
) -> None:
    """ADR-0005 §Decision: ``contract.constraints >= profile.envelope`` is
    validated once at promotion (and re-checked at binding), so a promoted
    contract can never again be the stricter of two ceilings. A contract
    below the profile fails closed with a message naming the superset
    requirement, the dimension, and both values.
    """
    resolver = _resolve_effective_resources()
    error_type = _resource_authority_error()

    # Pure seam: the resolver fail-closes rather than clamping a contract
    # that cannot contain the profile.
    with pytest.raises(error_type) as excinfo:
        resolver(
            request=dict(CPU_PROFILE),
            profile=dict(CPU_PROFILE),
            constraints=dict(CONTRACT_CONSTRAINTS),
            policy=dict(POLICY),
        )
    message = str(excinfo.value)
    assert 'superset' in message.lower()
    assert 'wallclock_minutes' in message
    assert '60' in message
    assert '30' in message

    # Engine integration: promotion of a sealed contract below the profile
    # fails closed exactly once with the same actionable framing.
    settings, store, cluster, _, original = orchestrator_bundle
    engine = ResearchOrchestrator(
        settings=settings,
        store=store,
        runtime=_ProfileProtocolRuntime(runner_image=RUNNER_IMAGE),
        workspaces=original.workspaces,
        contracts=original.contracts,
        contract_candidates=original.contract_candidates,
        policy=original.policy,
        cluster=cluster,
        discord=DisabledDiscordAdapter(),
    )
    run = engine.create_run(
        RunCreateRequest(objective='Reject a contract that excludes the profile.')
    )
    contract = _bind_profile_task_and_park_for_promotion(
        tmp_path,
        store,
        engine,
        run,
        resource_constraints=CONTRACT_CONSTRAINTS,
    )
    _save_approved_promotion_action(
        store,
        run_id=run.run_id,
        descriptor=contract.descriptor.model_dump(mode='json'),
        digest=contract.digest,
    )

    engine.resume_run(run.run_id, requested_by='test-human')

    failed = store.get_run(run.run_id)
    assert failed.state == RunState.FAILED
    conflicts = _events(
        store,
        run.run_id,
        'methodology.resource_authority_conflict',
    )
    assert len(conflicts) == 1
    reason = str(conflicts[0].payload['reason'])
    assert 'wallclock_minutes' in reason
    assert '60' in reason
    assert '30' in reason
    assert 'superset' in reason.lower()


@pytest.mark.xfail(
    strict=True,
    reason='ADR-0005 clamp unimplemented (T10); conflict class still reachable',
)
def test_residual_conflict_is_unrepresentable() -> None:
    """ADR-0005 §Decision and §Consequences: once every authority clamps,
    ``effective <= min(profile, constraints, policy)`` for every request, so
    the matrix simultaneously satisfies the profile envelope and the contract
    and the ``resource_authority_conflict`` dead-end class is unreachable by
    construction rather than merely detected.

    A contract that is not a superset of the profile is still fail-closed
    (test 3), but that is a promotion-time configuration decision, not a
    matrix revision dead end.
    """
    resolver = _resolve_effective_resources()
    error_type = _resource_authority_error()
    generous = {
        'cpu': 64,
        'memory_gib': 512,
        'gpus': 8,
        'wallclock_minutes': 10_080,
    }
    requests = [
        {'cpu': 1, 'memory_gib': 1, 'gpus': 0, 'wallclock_minutes': 5},
        {'cpu': 4, 'memory_gib': 8, 'gpus': 0, 'wallclock_minutes': 60},
        {'cpu': 64, 'memory_gib': 512, 'gpus': 8, 'wallclock_minutes': 10_080},
    ]
    profiles = [
        dict(profile.resources) for profile in RUNTIME_PROFILES.values()
    ]

    for profile in profiles:
        for request in requests:
            effective = _normalized(
                _effective(
                    resolver,
                    request=request,
                    profile=profile,
                    constraints=generous,
                    policy=POLICY,
                )
            )
            authorities = (request, profile, generous, POLICY)
            for dimension in (
                'cpu',
                'memory_gib',
                'gpus',
                'wallclock_minutes',
            ):
                caps = [
                    float(authority[dimension])
                    for authority in authorities
                    if dimension in authority
                ]
                # min over every authority is both the value and the proof
                # that the matrix fits every authority at once.
                assert effective[dimension] == min(caps)
            constraints = ResourceRequest.model_validate(effective)
            assert (
                profile_contract_resource_conflicts(
                    profile=profile,
                    constraints=constraints,
                )
                == []
            )

    # A profile/contract contradiction is fail-closed at promotion and never
    # becomes a matrix revision dead end.
    with pytest.raises(error_type):
        resolver(
            request=dict(CPU_PROFILE),
            profile=dict(CPU_PROFILE),
            constraints=dict(CONTRACT_CONSTRAINTS),
            policy=dict(POLICY),
        )
