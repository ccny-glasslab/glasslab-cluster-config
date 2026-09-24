"""Deterministic preflight gate before cluster submission.

Runs static, code-owning checks over the candidate config and the imported
workload source: methodology requirements from the approved evaluation contract
must be present as plain scalar/list values, the workload must statically write
the required metrics.json keys, and it must never create or score the
evaluator-owned outputs. The report fails closed: `passed` requires zero
errors, and every error is surfaced to the human reviewer.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
import yaml

from .evidence_resolver import EvidenceURIResolver
from .research_store import ResearchStore
from .schemas import (
    Claim,
    ExperimentMatrix,
    MIN_COMPARISON_SEEDS,
    ResolvedEvaluationContract,
    ResourceRequest,
    RunRecord,
)


class MethodologyRequirement(BaseModel):
    model_config = ConfigDict(extra='forbid')

    requirement_id: str = Field(min_length=1)
    config_path: str = Field(min_length=1)
    mode: Literal['comparison', 'decision']
    comparison_scope: Literal['within_job', 'across_jobs'] = 'within_job'
    minimum_distinct_values: int = Field(default=1, ge=1)
    maximum_distinct_values: int | None = Field(default=None, ge=1)
    description: str = Field(min_length=1)


class MatrixPreflightReport(BaseModel):
    model_config = ConfigDict(extra='forbid')

    passed: bool
    job_count: int
    checks: list[str] = Field(default_factory=list)
    comparisons: dict[str, list[str]] = Field(default_factory=dict)
    decisions: dict[str, list[str]] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    # Resolved comparison topology, surfaced to the agent review so a correct
    # within_job single-candidate/empty-overrides shape is not mistaken for the
    # #474 rejected shape.
    comparison_scope: Literal['within_job', 'across_jobs'] = 'within_job'
    comparison_topology: str = ''
    # A non-retryable report is a configuration contradiction, not a model
    # mistake: no revision, redraft, or retry can satisfy it, so the engine
    # parks the run for human resolution instead of spending budget on an
    # unsatisfiable matrix (issue #483).
    non_retryable: bool = False


class VerificationPreflightReport(BaseModel):
    model_config = ConfigDict(extra='forbid')

    passed: bool
    claim_count: int
    uri_count: int
    checks: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    unresolved_uris: list[str] = Field(default_factory=list)


EVALUATOR_OWNED_LITERALS = {
    # The workload emits evidence and metrics only; these outputs belong to the
    # immutable evaluator and must never be written or scored by workload code.
    'evaluation.json',
    'integrity_pass',
    'rubric_score',
}
ORCHESTRATOR_RESERVED_ARTIFACTS = frozenset({
    # The run-level across-jobs comparison is produced by the orchestrator after
    # all jobs are terminal. No contract or matrix may request it, and no
    # workload may write it, so a job artifact can never masquerade as (or
    # suppress) the authoritative comparison.
    'comparison.json',
})


def is_reserved_artifact(value: str) -> bool:
    # Reserved matching is by basename so './comparison.json',
    # 'sub/comparison.json', and '../reports/comparison.json' are all rejected,
    # and the fingerprint-versioned comparison-<fp>.json names are covered too.
    name = PurePosixPath(str(value).replace('\\', '/')).name
    if name in ORCHESTRATOR_RESERVED_ARTIFACTS:
        return True
    return name.startswith('comparison-') and name.endswith('.json')
SCANNED_SOURCE_SUFFIXES = {
    # Static-analysis scope is deliberately limited to code files that carry
    # logic; data files cannot be reasoned about statically.
    '.js',
    '.py',
    '.r',
    '.sh',
    '.ts',
}


# `_config_value` resolves a dotted key path from the base_config root, so a
# methodology requirement is materializable only when its first segment is this
# top-level experiment-dimensions namespace.
EXPERIMENT_DIMENSIONS_ROOT = 'experiment_dimensions'


def _config_value(config: dict[str, Any], dotted_path: str) -> Any:
    value: Any = config
    for component in dotted_path.split('.'):
        if not isinstance(value, dict) or component not in value:
            raise KeyError(dotted_path)
        value = value[component]
    return value


def _distinct_strings(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(str(item) for item in values))


def _load_config(path: Path) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f'candidate config is not valid YAML: {exc}') from exc
    if not isinstance(parsed, dict):
        raise ValueError('candidate config must contain a YAML object')
    return parsed


def _dict_keys(
    expression: ast.expr,
    assignments: dict[str, ast.expr],
    subscript_keys: dict[str, set[str]],
    function_return_keys: dict[str, set[str]] | None = None,
    *,
    resolving: frozenset[str] = frozenset(),
) -> set[str]:
    # Conservative static key resolution: dict literals, simple assignments,
    # string subscripts, and (optionally) known function returns. `resolving`
    # breaks cycles in self-referential assignments instead of recursing
    # forever.
    if isinstance(expression, ast.Name):
        if expression.id in resolving:
            return set()
        assigned = assignments.get(expression.id)
        keys = set(subscript_keys.get(expression.id, set()))
        if assigned is not None:
            keys.update(
                _dict_keys(
                    assigned,
                    assignments,
                    subscript_keys,
                    function_return_keys,
                    resolving=resolving | {expression.id},
                )
            )
        return keys
    if (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Name)
        and function_return_keys is not None
    ):
        return set(function_return_keys.get(expression.func.id, set()))
    if not isinstance(expression, ast.Dict):
        return set()
    keys: set[str] = set()
    for key, value in zip(expression.keys, expression.values, strict=True):
        if key is None:
            keys.update(
                _dict_keys(
                    value,
                    assignments,
                    subscript_keys,
                    function_return_keys,
                    resolving=resolving,
                )
            )
        elif isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.add(key.value)
    return keys


def _references_metrics_json(
    expression: ast.expr,
    assignments: dict[str, ast.expr],
    *,
    resolving: frozenset[str] = frozenset(),
) -> bool:
    if isinstance(expression, ast.Name):
        if expression.id in resolving:
            return False
        assigned = assignments.get(expression.id)
        return assigned is not None and _references_metrics_json(
            assigned,
            assignments,
            resolving=resolving | {expression.id},
        )
    # Match the exact filename, not a substring: ``metrics.json.bak`` and
    # ``not_metrics.json`` are different files and must not count as the
    # contract's metrics output.
    return any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and Path(node.value).name == 'metrics.json'
        for node in ast.walk(expression)
    )


def _is_metrics_write_path(
    expression: ast.expr,
    assignments: dict[str, ast.expr],
    renamed_metric_sources: frozenset[str],
) -> bool:
    # A path is a metrics write target when it references metrics.json or is a
    # temp file atomically renamed onto metrics.json.
    if (
        isinstance(expression, ast.Name)
        and expression.id in renamed_metric_sources
    ):
        return True
    return _references_metrics_json(expression, assignments)


def _opens_metrics_json_for_write(
    expression: ast.expr,
    assignments: dict[str, ast.expr],
    renamed_metric_sources: frozenset[str] = frozenset(),
) -> bool:
    """Whether ``expression`` is a call opening ``metrics.json`` for writing.

    Covers the builtin ``open(<path>, ...)`` and the ``Path`` method
    ``<path>.open(...)``, so both ``with open('metrics.json', 'w') as handle``
    and ``handle = (output_dir / 'metrics.json').open('w')`` bind a handle the
    later ``json.dump(<dict>, <handle>)`` scan can resolve. A handle opened on a
    temp file that is later renamed onto metrics.json counts too.
    """
    if not isinstance(expression, ast.Call):
        return False
    if expression.args and _is_metrics_write_path(
        expression.args[0],
        assignments,
        renamed_metric_sources,
    ):
        return True
    func = expression.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == 'open'
        and _is_metrics_write_path(
            func.value,
            assignments,
            renamed_metric_sources,
        )
    )


def _json_dumps_argument(
    expression: ast.expr,
    assignments: dict[str, ast.expr] | None = None,
    *,
    resolving: frozenset[str] = frozenset(),
) -> ast.Call | None:
    """Return the ``json.dumps(...)`` call serializing ``expression``, if any.

    A write target is commonly handed ``json.dumps(<dict>, ...) + '\\n'``, so a
    string concatenation is unwrapped before the dumps call is looked for. The
    serialized payload is also frequently bound to a variable first
    (``payload = json.dumps(<dict>)`` then ``handle.write(payload)``), so a
    ``Name`` is resolved through ``assignments`` when they are supplied.
    ``resolving`` breaks cycles in self-referential assignments.
    """
    if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
        return _json_dumps_argument(
            expression.left,
            assignments,
            resolving=resolving,
        ) or _json_dumps_argument(
            expression.right,
            assignments,
            resolving=resolving,
        )
    if isinstance(expression, ast.Name) and assignments is not None:
        if expression.id in resolving:
            return None
        assigned = assignments.get(expression.id)
        if assigned is None:
            return None
        return _json_dumps_argument(
            assigned,
            assignments,
            resolving=resolving | {expression.id},
        )
    if (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Attribute)
        and expression.func.attr == 'dumps'
    ):
        return expression
    return None


def _metrics_root_errors(
    tree: ast.AST,
    *,
    relative: str,
    required_metric_keys: list[str],
) -> list[str]:
    if not required_metric_keys:
        return []
    assignments: dict[str, ast.expr] = {}
    subscript_keys: dict[str, set[str]] = {}
    function_return_keys: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                assignments[target.id] = node.value
            elif (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and isinstance(target.slice, ast.Constant)
                and isinstance(target.slice.value, str)
            ):
                subscript_keys.setdefault(target.value.id, set()).add(
                    target.slice.value
                )
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            assignments[node.target.id] = node.value

    # Temp-file-then-rename writes (``tmp.write_text(...)`` followed by
    # ``os.replace(tmp, .../metrics.json)``) are atomic metrics writes; record
    # the temp variables so their writes resolve to metrics.json.
    rename_candidates: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        ):
            continue
        attr = node.func.attr
        if attr not in {'replace', 'rename', 'move'}:
            continue
        if len(node.args) >= 2 and _references_metrics_json(
            node.args[1],
            assignments,
        ):
            # os.replace(src, dst) / os.rename(src, dst) / shutil.move(src, dst)
            if isinstance(node.args[0], ast.Name):
                rename_candidates.add(node.args[0].id)
        if (
            attr in {'replace', 'rename'}
            and node.args
            and _references_metrics_json(node.args[0], assignments)
            and isinstance(node.func.value, ast.Name)
        ):
            # tmp.replace(dst) / tmp.rename(dst)
            rename_candidates.add(node.func.value.id)
    renamed_metric_sources = frozenset(rename_candidates)

    metric_handles: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and _opens_metrics_json_for_write(
                node.value,
                assignments,
                renamed_metric_sources,
            )
        ):
            metric_handles.add(node.targets[0].id)
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            if not isinstance(item.optional_vars, ast.Name):
                continue
            if _opens_metrics_json_for_write(
                item.context_expr,
                assignments,
                renamed_metric_sources,
            ):
                metric_handles.add(item.optional_vars.id)

    for function in (
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    ):
        local_assignments: dict[str, ast.expr] = {}
        local_subscript_keys: dict[str, set[str]] = {}
        returns: list[ast.expr] = []
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    local_assignments[target.id] = node.value
                elif (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                ):
                    local_subscript_keys.setdefault(
                        target.value.id,
                        set(),
                    ).add(target.slice.value)
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.value is not None
            ):
                local_assignments[node.target.id] = node.value
            elif isinstance(node, ast.Return) and node.value is not None:
                returns.append(node.value)
        returned_keys: set[str] = set()
        for expression in returns:
            returned_keys.update(
                _dict_keys(
                    expression,
                    local_assignments,
                    local_subscript_keys,
                    function_return_keys,
                )
            )
        function_return_keys[function.name] = returned_keys

    serialized_keys: set[str] = set()
    found_serialization = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == 'dump'
            and len(node.args) >= 2
            and (
                (
                    isinstance(node.args[1], ast.Name)
                    and node.args[1].id in metric_handles
                )
                # json.dump(<dict>, open('...metrics.json...', 'w')): the handle
                # need not be bound to a name first.
                or _opens_metrics_json_for_write(
                    node.args[1],
                    assignments,
                    renamed_metric_sources,
                )
            )
        ):
            # json.dump(<dict>, <metrics-handle>): the handle must have been
            # opened from something referencing 'metrics.json' earlier in the
            # walk.
            found_serialization = True
            serialized_keys.update(
                _dict_keys(
                    node.args[0],
                    assignments,
                    subscript_keys,
                    function_return_keys,
                )
            )
            continue
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {'write_text', 'write'}
            and node.args
        ):
            # Path.write_text(json.dumps(<dict>, ...)) / <metrics-handle>.write(
            # json.dumps(<dict>, ...)): the receiver must resolve to a
            # metrics.json write target - a Path referencing that filename, a
            # handle opened for it, or an inline ``open(...)`` call - and only a
            # json.dumps payload (possibly bound to a variable) counts as a JSON
            # write.
            receiver = node.func.value
            if not (
                _is_metrics_write_path(
                    receiver,
                    assignments,
                    renamed_metric_sources,
                )
                or (
                    isinstance(receiver, ast.Name)
                    and receiver.id in metric_handles
                )
                or _opens_metrics_json_for_write(
                    receiver,
                    assignments,
                    renamed_metric_sources,
                )
            ):
                continue
            dumps_call = _json_dumps_argument(node.args[0], assignments)
            if dumps_call is None or not dumps_call.args:
                continue
            found_serialization = True
            serialized_keys.update(
                _dict_keys(
                    dumps_call.args[0],
                    assignments,
                    subscript_keys,
                    function_return_keys,
                )
            )
    if not found_serialization:
        return [
            f'{relative} does not have a statically verifiable JSON write to '
            'metrics.json'
        ]
    missing = sorted(set(required_metric_keys) - serialized_keys)
    if not missing:
        return []
    return [
        f'{relative} serializes metrics.json without required root key(s): '
        + ', '.join(missing)
    ]


def _source_errors(
    source: Path,
    *,
    required_metric_keys: list[str],
    required_artifacts: list[str],
) -> list[str]:
    errors: list[str] = []
    referenced_tokens: set[str] = set()
    if not source.is_dir():
        return ['imported task source directory is missing']
    for path in sorted(source.rglob('*')):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.suffix.lower() not in SCANNED_SOURCE_SUFFIXES
        ):
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except UnicodeError:
            errors.append(f'source file is not UTF-8 text: {path.name}')
            continue
        relative = path.relative_to(source).as_posix()
        if path.suffix.lower() == '.py':
            try:
                tree = ast.parse(text, filename=relative)
            except SyntaxError as exc:
                errors.append(
                    f'Python syntax check failed for {relative}:{exc.lineno}: '
                    f'{exc.msg}'
                )
            else:
                referenced_tokens.update(
                    node.value
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                )
                if path.name == 'run.py':
                    errors.extend(
                        _metrics_root_errors(
                            tree,
                            relative=relative,
                            required_metric_keys=required_metric_keys,
                        )
                    )
        reserved = sorted(
            literal for literal in EVALUATOR_OWNED_LITERALS if literal in text
        )
        if reserved:
            errors.append(
                f'{relative} references evaluator-owned output '
                f'{", ".join(reserved)}; workloads emit evidence and metrics, '
                'while the immutable contract owns evaluation.json, '
                'integrity_pass, and rubric_score'
            )
        reserved_orchestrator = sorted(
            literal
            for literal in ORCHESTRATOR_RESERVED_ARTIFACTS
            if literal in text
        )
        if reserved_orchestrator:
            errors.append(
                f'{relative} references orchestrator-reserved output '
                f'{", ".join(reserved_orchestrator)}; the orchestrator writes '
                'the run-level comparison and a workload must not produce or '
                'score it'
            )
    reserved_artifacts = {
        artifact
        for artifact in required_artifacts
        if is_reserved_artifact(artifact)
    }
    for artifact in sorted(reserved_artifacts):
        errors.append(
            f'required artifact {artifact!r} is reserved by the orchestrator; '
            'the run-level comparison is produced by the orchestrator, not by '
            'the workload or a contract'
        )
    evaluator_owned = EVALUATOR_OWNED_LITERALS & set(required_artifacts)
    for artifact in required_artifacts:
        if artifact in evaluator_owned or artifact in reserved_artifacts:
            continue
        parts = [part for part in Path(artifact).parts if part not in {'.', '/'}]
        # A required artifact counts as "statically referenced" only when every
        # path segment (or the full path itself) appears as a string literal
        # somewhere in the scanned sources; anything less is reported as
        # missing so the workload cannot silently omit contract outputs.
        if all(
            any(part == literal or artifact == literal for literal in referenced_tokens)
            for part in parts
        ):
            continue
        errors.append(
            'workload source does not statically reference required artifact: '
            f'{artifact}'
        )
    return errors


@dataclass(frozen=True, slots=True)
class ResourceConstraintConflict:
    """One resource dimension where a profile exceeds contract constraints."""

    dimension: str
    profile_value: float
    contract_value: float

    def describe(self) -> str:
        return (
            f'{self.dimension} profile={self.profile_value:g} > '
            f'contract={self.contract_value:g}'
        )


def profile_contract_resource_conflicts(
    *,
    profile: Mapping[str, Any],
    constraints: ResourceRequest,
) -> list[ResourceConstraintConflict]:
    """Compare a preselected task resource profile with contract limits.

    The profile is the single authority for an imported benchmark's matrix
    resources (issue #483): the matrix must match it exactly. The contract's
    ``resource_constraints`` are a compatibility envelope the profile has to
    fit inside, not an independent ceiling that can override the profile.
    Dimensions the profile does not declare are ignored so hand-authored task
    bindings stay comparable; the compiled platform profiles always declare
    all four.
    """
    conflicts: list[ResourceConstraintConflict] = []
    for dimension in ('cpu', 'memory_gib', 'gpus', 'wallclock_minutes'):
        if dimension not in profile:
            continue
        profile_value = float(profile[dimension])
        contract_value = float(getattr(constraints, dimension))
        if profile_value > contract_value:
            conflicts.append(
                ResourceConstraintConflict(
                    dimension=dimension,
                    profile_value=profile_value,
                    contract_value=contract_value,
                )
            )
    return conflicts


@dataclass(frozen=True, slots=True)
class DeclaredBudgetConflict:
    """One declared contract budget that exceeds a deterministic limit."""

    scope: str
    declared_minutes: int
    limit_minutes: float

    def describe(self) -> str:
        return (
            f'manifest.budget.wallclock_minutes={self.declared_minutes} > '
            f'{self.scope}={self.limit_minutes:g}'
        )


def declared_budget_conflicts(
    *,
    manifest: Mapping[str, Any],
    constraints: ResourceRequest,
    profile: Mapping[str, Any] | None = None,
) -> list[DeclaredBudgetConflict]:
    """Check a declared contract budget against what the run can receive.

    ``manifest.budget`` and ``manifest.guardrails`` are informational: the
    run's wall-clock is the compiled task profile / matrix resources, never a
    value copied from the contract (issue #500). The declaration must still be
    honest: a ``wallclock_minutes`` larger than the contract's own
    ``resource_constraints`` or the task profile's wall-clock claims time the
    run can never receive, so it is rejected instead of approved. A smaller
    declaration is advisory and does not shrink the job.
    """
    raw_budget = manifest.get('budget')
    if raw_budget is None:
        return []
    if not isinstance(raw_budget, Mapping):
        raise ValueError('manifest.budget must be a JSON object when declared')
    raw_minutes = raw_budget.get('wallclock_minutes')
    if raw_minutes is None:
        return []
    if (
        isinstance(raw_minutes, bool)
        or not isinstance(raw_minutes, int)
        or raw_minutes < 1
    ):
        raise ValueError(
            'manifest.budget.wallclock_minutes must be a positive integer '
            'number of minutes'
        )
    limits: list[tuple[str, float]] = [
        ('contract resource_constraints', float(constraints.wallclock_minutes)),
    ]
    if profile is not None and 'wallclock_minutes' in profile:
        limits.append(
            ('task resource profile', float(profile['wallclock_minutes']))
        )
    return [
        DeclaredBudgetConflict(
            scope=scope,
            declared_minutes=raw_minutes,
            limit_minutes=limit,
        )
        for scope, limit in limits
        if raw_minutes > limit
    ]


def _task_spec_required_metric_keys(
    task_definition: Mapping[str, Any] | None,
) -> list[str]:
    # The generic evaluator reads task_spec.required_metric_keys from the job
    # payload; the contract's optional manifest list does not restate them, so
    # the static source scan must check both lists (issue #497).
    if not task_definition:
        return []
    task_spec = task_definition.get('task_spec')
    if not isinstance(task_spec, Mapping):
        return []
    keys = task_spec.get('required_metric_keys')
    if not isinstance(keys, list):
        return []
    return [key for key in keys if isinstance(key, str) and key]


def _as_metric_keys(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [key for key in value if isinstance(key, str) and key]


def _contract_output_schema_metric_keys(
    contract: ResolvedEvaluationContract,
) -> list[str]:
    """Metric roots the contract's sealed evaluator derives from ``metrics.json``.

    A task-specific contract expresses its metric contract in the sealed
    ``expected_output_schema`` rather than in ``manifest.required_metric_keys``
    (the promoted ``titanic-survival-methodology-v1`` is the live example): its
    evaluator reads a fixed set of metric roots from ``metrics.json`` and echoes
    them under the output's ``metrics`` object, so the schema's ``metrics`` keys
    are exactly the roots the workload must serialize. The task spec's keys, by
    contrast, are written once by the compiler model from ``problem.md`` and are
    only enforced by the generic evaluator, so they can drift from the bound
    contract (issue #492, finding A.5). The sealed schema is authoritative.
    """
    root = Path(contract.root_path).resolve()
    try:
        schema_path = (
            root / contract.descriptor.expected_output_schema
        ).resolve()
        if not schema_path.is_relative_to(root):
            return []
        schema = json.loads(schema_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        return []
    if not isinstance(schema, dict):
        return []
    properties = schema.get('properties')
    metrics = (
        properties.get('metrics') if isinstance(properties, dict) else None
    )
    if not isinstance(metrics, dict):
        return []
    keys = _as_metric_keys(metrics.get('required'))
    metric_properties = metrics.get('properties')
    if isinstance(metric_properties, dict):
        keys.extend(
            key for key in metric_properties if isinstance(key, str) and key
        )
    return list(dict.fromkeys(keys))


def _reconcile_required_metric_keys(
    *,
    contract: ResolvedEvaluationContract,
    task_definition: Mapping[str, Any] | None,
) -> tuple[list[str], list[str]]:
    """Return ``(required, superseded)`` metric roots for the static scan.

    The bound contract's sealed evaluator owns which roots must exist in
    ``metrics.json``, so its keys - ``manifest.required_metric_keys`` plus the
    sealed ``expected_output_schema`` - are authoritative whenever it declares
    any. The compiled task spec's keys remain the fallback for a contract that
    declares none (``generic-task-integrity-v1``), whose evaluator reads
    ``task_spec.required_metric_keys`` from the job payload (issue #497).

    A task-spec key that is absent from the contract is a stale compiled value:
    it is returned as ``superseded`` so preflight can surface the cross-layer
    disagreement in its checks, but it is not required - the bound evaluator
    never reads it, and enforcing it is what produced the byte-identical,
    non-converging matrix rejection in issue #492 (finding A.5).
    """
    contract_keys = list(
        dict.fromkeys(
            [
                *_as_metric_keys(
                    contract.descriptor.manifest.get('required_metric_keys')
                ),
                *_contract_output_schema_metric_keys(contract),
            ]
        )
    )
    task_spec_keys = _task_spec_required_metric_keys(task_definition)
    if not contract_keys:
        return task_spec_keys, []
    superseded = sorted(set(task_spec_keys) - set(contract_keys))
    return contract_keys, superseded


def resolve_comparison_scope(
    requirements: Sequence[MethodologyRequirement | Mapping[str, Any]],
) -> Literal['within_job', 'across_jobs']:
    # Single source for the matrix-topology rule, accepting either parsed
    # requirements or raw manifest mappings. At most one across_jobs comparison
    # is permitted per contract, so the presence of one selects the across-jobs
    # topology; anything else keeps legacy within-job behavior.
    for requirement in requirements:
        if isinstance(requirement, Mapping):
            mode = requirement.get('mode')
            scope = requirement.get('comparison_scope')
        else:
            mode = requirement.mode
            scope = requirement.comparison_scope
        if mode == 'comparison' and scope == 'across_jobs':
            return 'across_jobs'
    return 'within_job'


def comparison_scope_for_manifest(manifest: Mapping[str, Any]) -> str:
    raw_requirements = manifest.get('methodology_requirements', [])
    if not isinstance(raw_requirements, (list, tuple)):
        return 'within_job'
    return resolve_comparison_scope(
        [
            item
            for item in raw_requirements
            if isinstance(item, Mapping)
        ]
    )


def _comparison_topology_summary(
    comparison_scope: str,
    requirements: list[MethodologyRequirement],
) -> str:
    across_paths = sorted(
        requirement.config_path
        for requirement in requirements
        if (
            requirement.mode == 'comparison'
            and requirement.comparison_scope == 'across_jobs'
        )
    )
    within_paths = sorted(
        requirement.config_path
        for requirement in requirements
        if (
            requirement.mode == 'comparison'
            and requirement.comparison_scope == 'within_job'
        )
    )
    if not across_paths and not within_paths:
        return (
            'no comparison requirement: one `candidate` variant with empty '
            'overrides'
        )
    if comparison_scope == 'across_jobs':
        split = ', '.join(f'`{path}`' for path in across_paths)
        summary = (
            'across_jobs: one non-empty variant per compared methodology, each '
            f'overriding {split} with a distinct scalar; base_config holds one '
            'placeholder scalar at the split path(s)'
        )
        if within_paths:
            internal = ', '.join(f'`{path}`' for path in within_paths)
            summary += (
                f'; the within_job axes {internal} keep their full value lists '
                'in base_config and run inside each job, replicated by at least '
                f'{MIN_COMPARISON_SEEDS} matrix seeds'
            )
        else:
            summary += '; one seed per value is correct'
        return summary
    paths = ', '.join(f'`{path}`' for path in within_paths)
    return (
        'within_job: exactly one `candidate` variant with EMPTY overrides; the '
        f'full distinct list of compared values lives in base_config at {paths}; '
        f'use at least {MIN_COMPARISON_SEEDS} matrix seeds'
    )


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool))


def _across_jobs_base_config_error(
    requirement: MethodologyRequirement,
    configured_value: Any,
) -> str | None:
    if _is_scalar(configured_value):
        return None
    return (
        f'`{requirement.config_path}` must contain exactly one scalar value '
        'for an across_jobs comparison; the compared methods belong in the '
        'variant overrides, not in base_config'
    )


def _across_jobs_variant_errors(
    requirement: MethodologyRequirement,
    matrix: ExperimentMatrix,
) -> tuple[list[str], list[str]]:
    # across_jobs topology: every variant selects one methodology by overriding
    # the compared config_path with a distinct scalar; the union is the compared
    # set. Error text is deliberately free of variant names/values so the
    # non-convergence signature stays stable across identical rejections.
    config_path = requirement.config_path
    errors: list[str] = []
    values: list[str] = []
    for variant in matrix.variants:
        if not variant.overrides:
            errors.append(
                f'`{config_path}` requires a non-empty `overrides` object for '
                'an across_jobs comparison; an empty-override variant cannot '
                'select a compared methodology'
            )
            continue
        if config_path not in variant.overrides:
            errors.append(
                f'every variant must override `{config_path}` with a distinct '
                'scalar value for an across_jobs comparison'
            )
            continue
        override = variant.overrides[config_path]
        if not _is_scalar(override):
            errors.append(
                f'`{config_path}` override must be a distinct scalar value for '
                'an across_jobs comparison, not a list or object'
            )
            continue
        values.append(str(override))
    distinct = list(dict.fromkeys(values))
    count = len(distinct)
    if count < requirement.minimum_distinct_values:
        errors.append(
            f'`{config_path}` across_jobs comparison requires at least '
            f'{requirement.minimum_distinct_values} distinct variant override '
            f'value(s); found {count}'
        )
    if (
        requirement.maximum_distinct_values is not None
        and count > requirement.maximum_distinct_values
    ):
        errors.append(
            f'`{config_path}` across_jobs comparison allows at most '
            f'{requirement.maximum_distinct_values} distinct variant override '
            f'value(s); found {count}'
        )
    return errors, distinct


def preflight_matrix(
    *,
    run: RunRecord,
    matrix: ExperimentMatrix,
    contract: ResolvedEvaluationContract,
) -> MatrixPreflightReport:
    workspace = Path(run.beaker_workspace).resolve()
    base_config = (workspace / matrix.base_config).resolve()
    errors: list[str] = []
    checks: list[str] = []
    comparisons: dict[str, list[str]] = {}
    decisions: dict[str, list[str]] = {}

    if not base_config.is_relative_to(workspace) or not base_config.is_file():
        errors.append(
            'base_config does not exist inside the Beaker workspace: '
            f'{matrix.base_config}'
        )
        config: dict[str, Any] = {}
    else:
        try:
            config = _load_config(base_config)
            checks.append(f'candidate config parsed: {matrix.base_config}')
        except ValueError as exc:
            config = {}
            errors.append(str(exc))

    raw_requirements = contract.descriptor.manifest.get(
        'methodology_requirements',
        [],
    )
    try:
        requirements = [
            MethodologyRequirement.model_validate(item)
            for item in raw_requirements
        ]
    except ValueError as exc:
        errors.append(f'evaluation contract methodology requirements are invalid: {exc}')
        requirements = []

    for requirement in requirements:
        try:
            configured_value = _config_value(config, requirement.config_path)
        except KeyError:
            errors.append(
                f'missing methodology setting `{requirement.config_path}`: '
                f'{requirement.description}'
            )
            continue
        if isinstance(configured_value, dict):
            # Reject wrapped metadata objects outright: a requirement value
            # must be a plain scalar or list so the deterministic check sees
            # exactly what the job will consume.
            errors.append(
                f'`{requirement.config_path}` must directly contain a scalar '
                'or list of values, not a metadata object; do not wrap values '
                'beneath `description` or `values`'
            )
            continue
        if (
            requirement.mode == 'comparison'
            and requirement.comparison_scope == 'across_jobs'
        ):
            base_config_error = _across_jobs_base_config_error(
                requirement,
                configured_value,
            )
            if base_config_error is not None:
                errors.append(base_config_error)
            variant_errors, values = _across_jobs_variant_errors(
                requirement,
                matrix,
            )
            errors.extend(variant_errors)
            comparisons[requirement.requirement_id] = values
            continue
        values = _distinct_strings(configured_value)
        count = len(values)
        if count < requirement.minimum_distinct_values:
            errors.append(
                f'`{requirement.config_path}` requires at least '
                f'{requirement.minimum_distinct_values} distinct value(s); '
                f'found {count}'
            )
        if (
            requirement.maximum_distinct_values is not None
            and count > requirement.maximum_distinct_values
        ):
            errors.append(
                f'`{requirement.config_path}` allows at most '
                f'{requirement.maximum_distinct_values} distinct value(s); '
                f'found {count}'
            )
        target = comparisons if requirement.mode == 'comparison' else decisions
        target[requirement.requirement_id] = values

    if requirements:
        checks.append(
            f'validated {len(requirements)} contract methodology requirement(s)'
        )

    reserved_requested = sorted(
        {
            artifact
            for artifact in matrix.required_artifacts
            if is_reserved_artifact(artifact)
        }
    )
    if reserved_requested:
        errors.append(
            'experiment matrix may not request orchestrator-reserved '
            f'artifact(s): {", ".join(reserved_requested)}; the '
            'orchestrator owns the run-level comparison'
        )

    if run.task_definition:
        source = (
            workspace / str(run.task_definition['source_subdirectory'])
        ).resolve()
        if not source.is_relative_to(workspace):
            errors.append('imported task source directory escapes the workspace')
        else:
            required_metric_keys, superseded_metric_keys = (
                _reconcile_required_metric_keys(
                    contract=contract,
                    task_definition=run.task_definition,
                )
            )
            if superseded_metric_keys:
                # Surface the cross-layer disagreement without making the
                # matrix unsatisfiable: the bound evaluator never reads these
                # roots, so requiring them is the #492 non-convergence bug.
                checks.append(
                    'bound contract owns metrics.json roots; stale compiled '
                    'task-spec key(s) superseded: '
                    + ', '.join(superseded_metric_keys)
                )
            source_findings = _source_errors(
                source,
                required_metric_keys=required_metric_keys,
                required_artifacts=list(contract.descriptor.required_artifacts),
            )
            errors.extend(source_findings)
            if not source_findings:
                checks.append(
                    'workspace syntax and evaluator-output ownership checks passed'
                )

    configured_seeds = config.get('seeds')
    if (
        isinstance(configured_seeds, list)
        and len(configured_seeds) > 1
        and configured_seeds == matrix.seeds
    ):
        # Enforces the reproducibility rule from AGENTS.md: an internal
        # multi-seed stability analysis must run inside one job, while outer
        # matrix seeds create independent replicated jobs. Duplicating the same
        # list in both places silently inflates the job count.
        errors.append(
            'candidate config and outer experiment matrix contain the same '
            'multi-seed list; internal stability seeds must run inside one job, '
            'while matrix seeds create independent replicated jobs'
        )

    job_count = len(matrix.variants) * len(matrix.seeds)
    checks.append(f'deterministic expansion produces {job_count} job(s)')

    has_within_job_comparison = any(
        r.mode == 'comparison' and r.comparison_scope == 'within_job'
        for r in requirements
    )
    if has_within_job_comparison and len(matrix.seeds) < MIN_COMPARISON_SEEDS:
        errors.append(
            f'comparison contract requires at least {MIN_COMPARISON_SEEDS} '
            f'matrix seeds; found {len(matrix.seeds)}'
        )

    comparison_scope = resolve_comparison_scope(requirements)
    return MatrixPreflightReport(
        passed=not errors,
        job_count=job_count,
        checks=checks,
        comparisons=comparisons,
        decisions=decisions,
        errors=errors,
        comparison_scope=comparison_scope,
        comparison_topology=_comparison_topology_summary(
            comparison_scope,
            requirements,
        ),
    )


def preflight_verification_evidence(
    *,
    claims: list[Claim],
    store: ResearchStore,
) -> VerificationPreflightReport:
    """Deterministic verification preflight over claim evidence URIs.

    Independent verification is satisfied by this deterministic
    claim-to-evidence layer, not by prompt-only same-model verification: every
    evidence URI cited by the candidate's claims must resolve to an
    authoritative store record before the verification evidence is accepted.
    Unresolved artifact://, job://, event://, and knowledge:// URIs fail the
    preflight with a specific error listing the offending URIs.
    """
    resolver = EvidenceURIResolver(store)
    errors: list[str] = []
    unresolved_uris: list[str] = []
    uri_count = 0
    for claim in claims:
        for uri in claim.evidence:
            uri_count += 1
            result = resolver.resolve(uri)
            if not result.resolved:
                unresolved_uris.append(uri)
                errors.append(f'evidence URI unresolved: {uri} ({result.error})')
    checks: list[str] = []
    if claims:
        checks.append(
            f'resolved {uri_count} evidence URI(s) across {len(claims)} claim(s)'
        )
    return VerificationPreflightReport(
        passed=not errors,
        claim_count=len(claims),
        uri_count=uri_count,
        checks=checks,
        errors=errors,
        unresolved_uris=unresolved_uris,
    )
