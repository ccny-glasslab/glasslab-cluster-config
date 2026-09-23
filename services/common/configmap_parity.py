"""Generic tracked-ConfigMap vs pydantic-settings drift audit.

A Kubernetes ConfigMap env entry overrides a pydantic-settings default, so a
stale tracked manifest silently defeats a deliberate change in ``app/config.py``
even when the code, its comment, and its unit test all agree. The original guard
caught this for exactly one key (``opencode_turn_timeout_seconds``); this module
generalizes the invariant to every key the ConfigMap sets.

The audit is pure: given a ``BaseSettings`` subclass, the parsed ConfigMap
``data`` mapping, and an explicit override allowlist, it returns one violation
message per offending ConfigMap key (an unknown key that maps to no field is a
violation too). No environment injection, no ``Settings(**configmap)``
construction -- both would trip unrelated model validators.

Natural invariant per field type, when no override is declared:

* ``bool``           -> deployed equals the code default
* ``int`` / ``float`` -> deployed >= code default (widen, never narrow)
* ``str`` / ``Literal`` -> deployed equals the code default
* ``list`` / ``tuple`` -> deployed set equals the code-default set
* ``dict``           -> deployed equals the code default

An ``Override`` relaxes or pins the invariant for one key. Every override MUST
carry a non-empty ``reason``: the reason is what turns a silent "allow" into a
decision a human can review.
"""

from __future__ import annotations

import json
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

import yaml
from pydantic import TypeAdapter
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, EnvSettingsSource

OverrideKind = Literal['eq', 'gte', 'allowlist', 'set']

_OPTIONAL_UNION_ORIGINS = (Union, types.UnionType)


@dataclass(frozen=True, slots=True)
class Override:
    """A reviewed exception to the natural per-type invariant.

    ``eq``        deployed must equal ``expected`` (required literal).
    ``gte``       deployed (numeric) must be >= the code default.
    ``allowlist`` deployed raw string must be in ``allowed``.
    ``set``       deployed list-as-set must equal ``expected_set``.
    """

    kind: OverrideKind
    reason: str
    expected: str | None = None
    allowed: frozenset[str] = frozenset()
    expected_set: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError('override reason must be non-empty')
        if self.kind == 'eq' and self.expected is None:
            raise ValueError('eq override requires expected')
        if self.kind == 'allowlist' and not self.allowed:
            raise ValueError('allowlist override requires allowed')
        if self.kind == 'set' and not self.expected_set:
            raise ValueError('set override requires expected_set')


def load_configmap_data(path: Path) -> dict[str, str]:
    """Return the ``data`` mapping of the first truthy YAML document."""
    for document in yaml.safe_load_all(path.read_text(encoding='utf-8')):
        if document:
            data = document.get('data', {})
            return {str(key): str(value) for key, value in data.items()}
    raise AssertionError(f'no ConfigMap document found in {path}')


def env_name_index(
    settings_cls: type[BaseSettings],
) -> dict[str, tuple[str, FieldInfo]]:
    """Map every accepted UPPER env name to its (field_name, FieldInfo)."""
    config = settings_cls.model_config
    source = EnvSettingsSource(
        settings_cls,
        case_sensitive=config.get('case_sensitive'),
        env_prefix=config.get('env_prefix'),
    )
    index: dict[str, tuple[str, FieldInfo]] = {}
    for field_name, field in settings_cls.model_fields.items():
        for _key, env_name, _complex in source._extract_field_info(
            field, field_name
        ):
            index[env_name.upper()] = (field_name, field)
    return index


def _strip_optional(annotation: Any) -> Any:
    if get_origin(annotation) in _OPTIONAL_UNION_ORIGINS:
        members = [
            arg for arg in get_args(annotation) if arg is not type(None)
        ]
        if len(members) == 1:
            return members[0]
    return annotation


def _family(annotation: Any) -> str:
    plain = _strip_optional(annotation)
    origin = get_origin(plain)
    if plain is bool:
        return 'bool'
    if plain is int:
        return 'int'
    if plain is float:
        return 'float'
    if plain is str:
        return 'str'
    if origin is Literal:
        return 'literal'
    if origin in (list, set, frozenset):
        return 'list'
    if origin is tuple:
        return 'tuple'
    if origin is dict:
        return 'dict'
    return 'other'


def _natural_kind(family: str) -> str:
    if family in ('int', 'float'):
        return 'gte'
    if family in ('list', 'tuple'):
        return 'set'
    return 'eq'


def code_default(field: FieldInfo) -> Any:
    return field.get_default(call_default_factory=True)


def _parse(annotation: Any, raw: str) -> Any:
    # NoDecode list fields carry their own comma-splitting validator, so the
    # ConfigMap spelling is split here rather than through TypeAdapter.
    family = _family(annotation)
    if family in ('list', 'tuple'):
        return [item.strip() for item in raw.split(',') if item.strip()]
    if family == 'dict':
        return json.loads(raw)
    if family == 'literal':
        return raw
    return TypeAdapter(annotation).validate_python(raw)


def _violation(
    key: str,
    field_name: str,
    raw: str,
    annotation: Any,
    default: Any,
    override: Override | None,
) -> str | None:
    try:
        deployed = _parse(annotation, raw)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        return (
            f'{key} ({field_name}): cannot parse deployed value {raw!r} for '
            f'{annotation}: {exc}'
        )

    kind = override.kind if override is not None else _natural_kind(
        _family(annotation)
    )
    reason = f' -- {override.reason}' if override is not None else ''

    if kind == 'allowlist':
        if raw.strip() not in override.allowed:  # type: ignore[union-attr]
            return (
                f'{key} ({field_name}): deployed {raw!r} is not one of the '
                f'allowed values {sorted(override.allowed)}{reason}'  # type: ignore[union-attr]
            )
        return None

    if kind == 'eq':
        expected = override.expected if override is not None else None
        if expected is None:
            expected_value = default
            expected_display = default
        else:
            expected_value = _parse(annotation, expected)
            expected_display = expected
        if deployed != expected_value:
            return (
                f'{key} ({field_name}): deployed {raw!r} != required '
                f'{expected_display!r}{reason}'
            )
        return None

    if kind == 'gte':
        if float(deployed) < float(default):
            return (
                f'{key} ({field_name}): deployed {deployed!r} narrows the code '
                f'default {default!r}; the deployment may widen but never '
                f'narrow{reason}'
            )
        return None

    if kind == 'set':
        expected_set = override.expected_set if override is not None else None
        if not expected_set:
            expected_set = tuple(str(item) for item in default)
        deployed_set = {str(item) for item in deployed}
        if deployed_set != set(expected_set):
            return (
                f'{key} ({field_name}): deployed set {sorted(deployed_set)} != '
                f'required set {sorted(expected_set)}{reason}'
            )
        return None

    raise AssertionError(f'unhandled override kind: {kind}')


def audit(
    settings_cls: type[BaseSettings],
    data: dict[str, str],
    overrides: dict[str, Override],
) -> dict[str, str]:
    """Return ``configmap_key -> violation message`` for every offending key."""
    index = env_name_index(settings_cls)
    violations: dict[str, str] = {}
    for raw_key, raw_value in data.items():
        key = raw_key.upper()
        entry = index.get(key)
        if entry is None:
            violations[raw_key] = (
                f'{raw_key}: no {settings_cls.__name__} field maps to this '
                'ConfigMap key; fix the typo or the field name'
            )
            continue
        field_name, field = entry
        violation = _violation(
            raw_key,
            field_name,
            str(raw_value),
            field.annotation,
            code_default(field),
            overrides.get(key),
        )
        if violation:
            violations[raw_key] = violation
    return violations


def validate_overrides(
    settings_cls: type[BaseSettings],
    data: dict[str, str],
    overrides: dict[str, Override],
) -> list[str]:
    """Return problems with the override table itself (meta-test support)."""
    index = env_name_index(settings_cls)
    configmap_keys = {key.upper() for key in data}
    problems: list[str] = []
    for key, override in overrides.items():
        normalized = key.upper()
        if not override.reason.strip():
            problems.append(f'{key}: override reason is empty')
        if normalized not in index:
            problems.append(
                f'{key}: no {settings_cls.__name__} field maps to this override '
                'key'
            )
        if normalized not in configmap_keys:
            problems.append(
                f'{key}: override names a key absent from the tracked ConfigMap'
            )
    return problems
