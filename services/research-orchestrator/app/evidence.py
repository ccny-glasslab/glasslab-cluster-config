"""Compact evidence snapshots for research-agent turns (issue #93).

Projects jobs and artifacts into bounded summaries and attaches per-phase
artifact excerpts, deduplicated by content digest, with a hard serialized-size
budget enforced by deterministic trimming. The budget measures the exact
production serialization the engine embeds in agent prompts
(``serialize_evidence``), counted as encoded UTF-8 bytes.
"""

from __future__ import annotations

import json
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any

from .config import Settings
from .research_store import ResearchStore
from .schemas import ArtifactRecord, JobRecord


class EvidencePhase(StrEnum):
    ANALYSIS = 'analysis'
    VERIFICATION = 'verification'
    REPORT = 'report'


_PHASE_FILENAMES: dict[EvidencePhase, frozenset[str]] = {
    EvidencePhase.ANALYSIS: frozenset({
        'runner.log', 'status.json', 'evaluation.json', 'metrics.json',
        'metrics.csv', 'fairness.csv',
    }),
    EvidencePhase.VERIFICATION: frozenset({
        'status.json', 'evaluation.json', 'metrics.json', 'report.md',
    }),
    EvidencePhase.REPORT: frozenset({'evaluation.json', 'metrics.json'}),
}

_VERBATIM_FILENAMES = frozenset({'evaluation.json', 'metrics.json'})
_PRIORITY_2_FILENAMES = frozenset({'runner.log', 'metrics.csv', 'fairness.csv'})
_PRIORITY_1_FILENAMES = frozenset({'status.json', 'report.md'})

_ANALYSIS_JOB_KEYS = frozenset({
    'job_id', 'run_id', 'action_id', 'job_name', 'kubernetes_uid',
    'external_run_id', 'status', 'exit_information', 'variant_name', 'seed',
    'evaluation_contract_id', 'evaluation_contract_version',
    'evaluation_contract_digest', 'created_at', 'updated_at',
})
_COMPACT_JOB_KEYS = frozenset({
    'job_id', 'action_id', 'status', 'variant_name', 'seed',
})
_ARTIFACT_KEYS = frozenset({'artifact_id', 'job_id', 'type', 'uri', 'sha256'})

_TRUNCATION_NOTE = (
    'evidence snapshot exceeded evidence_snapshot_max_bytes; complete '
    'artifacts remain in the durable record'
)
_MAX_OMITTED_URIS = 25


def serialize_evidence(snapshot: dict[str, Any]) -> str:
    """The exact JSON the engine embeds in agent prompts.

    Must stay in sync with the engine call sites (they import this function);
    the size budget is measured on its UTF-8 encoding, so the prompt can never
    exceed the configured cap by a serialization-shape mismatch.
    """
    return json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False)


def evidence_byte_size(snapshot: dict[str, Any]) -> int:
    return len(serialize_evidence(snapshot).encode('utf-8'))


def _project_job(job: JobRecord, phase: EvidencePhase) -> dict[str, Any]:
    keys = (
        _ANALYSIS_JOB_KEYS
        if phase == EvidencePhase.ANALYSIS
        else _COMPACT_JOB_KEYS
    )
    return job.model_dump(mode='json', include=keys)


def _artifact_excerpt(
    settings: Settings,
    artifact: ArtifactRecord,
    filename: str,
) -> dict[str, Any] | None:
    shared_root = Path(settings.shared_mount_root).resolve()
    path = (shared_root / Path(artifact.uri)).resolve()
    if not path.is_relative_to(shared_root) or not path.is_file():
        return None
    digest = sha256()
    with path.open('rb') as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != artifact.sha256:
        return {
            'uri': f'artifact://{artifact.uri}',
            'type': artifact.type,
            'sha256': artifact.sha256,
            'content_unavailable': 'artifact digest mismatch',
        }
    return _read_excerpt(
        settings, artifact, path, filename, path.stat().st_size
    )


def _read_excerpt(
    settings: Settings,
    artifact: ArtifactRecord,
    path: Path,
    filename: str,
    size: int,
) -> dict[str, Any]:
    verbatim = filename in _VERBATIM_FILENAMES
    maximum = (
        settings.evidence_verbatim_max_bytes
        if verbatim
        else settings.evidence_excerpt_max_bytes
    )
    if verbatim and size > maximum:
        return {
            'uri': f'artifact://{artifact.uri}',
            'type': artifact.type,
            'sha256': artifact.sha256,
            'digest_verified': True,
            'size_bytes': size,
            'content_omitted': f'artifact://{artifact.uri}',
        }
    tail = not verbatim and filename == 'runner.log' and size > maximum
    with path.open('rb') as handle:
        if tail:
            handle.seek(-maximum, 2)
        content = handle.read(maximum)
    text = content.decode('utf-8', errors='replace')
    parsed: Any = text
    if Path(filename).suffix == '.json' and size <= maximum:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = text
    return {
        'uri': f'artifact://{artifact.uri}',
        'type': artifact.type,
        'sha256': artifact.sha256,
        'digest_verified': True,
        'size_bytes': size,
        'truncated': size > maximum,
        'excerpt_position': 'full' if verbatim else 'tail' if tail else 'head',
        'content': parsed,
    }


def _entry_filename(entry: dict[str, Any]) -> str:
    return Path(str(entry['uri']).split('://', 1)[-1]).name


def _drop_candidates(
    jobs: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    contents: list[dict[str, Any]],
) -> list[tuple[str, str, str]]:
    # Least-protected first: logs/CSVs, then the artifact inventory, then job
    # summaries, then status/report contents, and verbatim evaluator/metrics
    # contents last (they must survive longest).
    candidates: list[tuple[str, str, str]] = []
    for filenames in (_PRIORITY_2_FILENAMES,):
        for entry in sorted(
            (e for e in contents if _entry_filename(e) in filenames),
            key=lambda e: str(e['uri']),
        ):
            candidates.append(
                ('artifact_contents', str(entry['uri']), str(entry['uri']))
            )
    for entry in sorted(artifacts, key=lambda e: str(e['uri'])):
        candidates.append(
            ('artifacts', str(entry['uri']), f"artifact://{entry['uri']}")
        )
    for entry in sorted(jobs, key=lambda e: str(e['job_id'])):
        candidates.append(
            ('jobs', str(entry['job_id']), str(entry['job_id']))
        )
    for filenames in (_PRIORITY_1_FILENAMES, _VERBATIM_FILENAMES):
        for entry in sorted(
            (e for e in contents if _entry_filename(e) in filenames),
            key=lambda e: str(e['uri']),
        ):
            candidates.append(
                ('artifact_contents', str(entry['uri']), str(entry['uri']))
            )
    return candidates


def _truncation_note(dropped: list[str]) -> dict[str, Any]:
    note: dict[str, Any] = {
        'note': _TRUNCATION_NOTE,
        'omitted_uris': dropped[:_MAX_OMITTED_URIS],
        'omitted_count': len(dropped),
    }
    if len(dropped) > _MAX_OMITTED_URIS:
        note['omitted_more_count'] = len(dropped) - _MAX_OMITTED_URIS
    return note


def _drop_content_entry(
    snapshot: dict[str, Any],
    dependents_by_rep: dict[str, list[str]],
    key: str,
) -> list[str]:
    """Remove one content entry plus any duplicates that reference it.

    A duplicate's ``duplicate_of`` must never point at content that trimming
    removed: dropping a representative also drops its dependents so no retained
    reference dangles. The representative is removed first and dependents are
    looked up fresh afterwards, so a dependent sharing the representative's uri
    can never be matched against it. Returns the removed uris so the caller can
    record them exactly once in the truncation note.
    """
    entry = next(
        (e for e in snapshot['artifact_contents'] if e['uri'] == key),
        None,
    )
    if entry is None:
        return []
    snapshot['artifact_contents'].remove(entry)
    removed = [key]
    for dependent_uri in dependents_by_rep.get(key, []):
        dependent = next(
            (e for e in snapshot['artifact_contents'] if e['uri'] == dependent_uri),
            None,
        )
        if dependent is not None:
            snapshot['artifact_contents'].remove(dependent)
            removed.append(str(dependent['uri']))
    return removed


def build_evidence_snapshot(
    settings: Settings,
    store: ResearchStore,
    run_id: str,
    phase: EvidencePhase = EvidencePhase.ANALYSIS,
) -> dict[str, Any]:
    allowed = _PHASE_FILENAMES[phase]
    jobs = [_project_job(job, phase) for job in store.list_jobs(run_id)]
    # The inventory is phase-scoped like the contents: metadata for artifacts
    # whose content the phase never receives is not phase-relevant evidence.
    artifacts = [
        artifact.model_dump(mode='json', include=_ARTIFACT_KEYS)
        for artifact in store.list_artifacts(run_id)
        if Path(artifact.uri).name in allowed
    ]
    contents: list[dict[str, Any]] = []
    first_uri_by_digest: dict[tuple[str, str], str] = {}
    dependents_by_rep: dict[str, list[str]] = {}
    for artifact in store.list_artifacts(run_id):
        filename = Path(artifact.uri).name
        if filename not in allowed:
            continue
        excerpt = _artifact_excerpt(settings, artifact, filename)
        if excerpt is None:
            continue
        key = (artifact.sha256, artifact.type)
        if 'content' in excerpt:
            first_uri = first_uri_by_digest.get(key)
            if first_uri is None:
                first_uri_by_digest[key] = str(excerpt['uri'])
            else:
                excerpt.pop('content')
                excerpt['duplicate_of'] = first_uri
                dependents_by_rep.setdefault(first_uri, []).append(
                    str(excerpt['uri'])
                )
        contents.append(excerpt)
    snapshot: dict[str, Any] = {
        'jobs': jobs,
        'artifacts': artifacts,
        'artifact_contents': contents,
    }
    candidates = _drop_candidates(jobs, artifacts, contents)
    dropped: list[str] = []
    dropped_set: set[str] = set()
    while candidates:
        provisional = dict(snapshot)
        if dropped:
            provisional['truncation'] = _truncation_note(dropped)
        if evidence_byte_size(provisional) <= settings.evidence_snapshot_max_bytes:
            break
        kind, key, identifier = candidates.pop(0)
        if kind == 'artifact_contents':
            for removed_uri in _drop_content_entry(
                snapshot, dependents_by_rep, key
            ):
                if removed_uri not in dropped_set:
                    dropped_set.add(removed_uri)
                    dropped.append(removed_uri)
        elif kind == 'artifacts':
            snapshot['artifacts'] = [
                item for item in snapshot['artifacts'] if str(item['uri']) != key
            ]
            if identifier not in dropped_set:
                dropped_set.add(identifier)
                dropped.append(identifier)
        else:
            snapshot['jobs'] = [
                item for item in snapshot['jobs'] if str(item['job_id']) != key
            ]
            if identifier not in dropped_set:
                dropped_set.add(identifier)
                dropped.append(identifier)
    if dropped:
        snapshot['truncation'] = _truncation_note(dropped)
        # Hard guarantee: the note itself adds bytes; if the final
        # serialization still exceeds the cap (pathological tiny caps), keep
        # summarizing the omitted-URI list until it fits.
        note = snapshot['truncation']
        while (
            note['omitted_uris']
            and evidence_byte_size(snapshot)
            > settings.evidence_snapshot_max_bytes
        ):
            note['omitted_uris'] = note['omitted_uris'][: len(note['omitted_uris']) // 2]
            if len(note['omitted_uris']) < note['omitted_count']:
                note['omitted_more_count'] = (
                    note['omitted_count'] - len(note['omitted_uris'])
                )
        # Floor: with every list empty and the note reduced to its fixed text
        # plus a count, the serialized snapshot is ~251 bytes. Settings rejects
        # caps below EVIDENCE_SNAPSHOT_MIN_BYTES (1024), so this halving loop
        # only runs defensively when a cap was mutated after construction.
    return snapshot
