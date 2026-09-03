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
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
import yaml

from .schemas import (
    ExperimentMatrix,
    MIN_COMPARISON_SEEDS,
    ResolvedEvaluationContract,
    RunRecord,
)


class MethodologyRequirement(BaseModel):
    model_config = ConfigDict(extra='forbid')

    requirement_id: str = Field(min_length=1)
    config_path: str = Field(min_length=1)
    mode: Literal['comparison', 'decision']
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


EVALUATOR_OWNED_LITERALS = {
    # The workload emits evidence and metrics only; these outputs belong to the
    # immutable evaluator and must never be written or scored by workload code.
    'evaluation.json',
    'integrity_pass',
    'rubric_score',
}
SCANNED_SOURCE_SUFFIXES = {
    # Static-analysis scope is deliberately limited to code files that carry
    # logic; data files cannot be reasoned about statically.
    '.js',
    '.py',
    '.r',
    '.sh',
    '.ts',
}


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
    return 'metrics.json' in ast.unparse(expression)


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
    metric_handles: set[str] = set()
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
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            if (
                not isinstance(item.optional_vars, ast.Name)
                or not isinstance(item.context_expr, ast.Call)
                or not item.context_expr.args
            ):
                continue
            if _references_metrics_json(
                item.context_expr.args[0],
                assignments,
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
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Attribute)
            or node.func.attr != 'dump'
            or len(node.args) < 2
            or not isinstance(node.args[1], ast.Name)
            or node.args[1].id not in metric_handles
        ):
            continue
        # Only json.dump(<dict>, <metrics-handle>) counts as a metrics.json
        # serialization; the handle must have been opened from something
        # referencing 'metrics.json' earlier in the walk.
        found_serialization = True
        serialized_keys.update(
            _dict_keys(
                node.args[0],
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
    evaluator_owned = EVALUATOR_OWNED_LITERALS & set(required_artifacts)
    for artifact in required_artifacts:
        if artifact in evaluator_owned:
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

    if run.task_definition:
        source = (
            workspace / str(run.task_definition['source_subdirectory'])
        ).resolve()
        if not source.is_relative_to(workspace):
            errors.append('imported task source directory escapes the workspace')
        else:
            source_findings = _source_errors(
                source,
                required_metric_keys=list(
                    contract.descriptor.manifest.get(
                        'required_metric_keys',
                        [],
                    )
                ),
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

    has_comparison_mode = any(r.mode == 'comparison' for r in requirements)
    if has_comparison_mode and len(matrix.seeds) < MIN_COMPARISON_SEEDS:
        errors.append(
            f'comparison contract requires at least {MIN_COMPARISON_SEEDS} '
            f'matrix seeds; found {len(matrix.seeds)}'
        )

    return MatrixPreflightReport(
        passed=not errors,
        job_count=job_count,
        checks=checks,
        comparisons=comparisons,
        decisions=decisions,
        errors=errors,
    )
