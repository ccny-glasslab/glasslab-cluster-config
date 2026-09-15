"""Prompt-budget tests for evidence compaction on the verify/report turns (#430).

PR #387 offloaded the full evidence snapshot to the agent workspace and left
only a content-free digest in the prompt. These tests lock that shape for the
verify (``HONEYDEW_VERIFYING``) and report (``HONEYDEW_WRITING_REPORT``) turns,
which are the dominant context cost on the ``.17`` Coder host, and record the
measured token drop from inlining the full snapshot to inlining the digest.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from app.evidence import EvidencePhase, serialize_evidence
from app.knowledge_manager import estimate_tokens
from app.schemas import ArtifactRecord, RunCreateRequest, TurnKind

# Distinctive raw marker: a regression to inlining full artifact contents would
# surface this string in the assembled prompt.
_RAW_MARKER = 'RAW-EVIDENCE-MARKER-430'

# Two artifacts with distinct digests so both contents attach (sha256 dedup only
# collapses identical content). Each payload stays under the 64 KB verbatim cap.
_METRICS_ENTRIES = 2000
_EVALUATION_ENTRIES = 2400


def _artifact_payload(entries: int) -> bytes:
    payload = {f'metric_{i}': i * 1.5 for i in range(entries)}
    payload[_RAW_MARKER] = True
    return json.dumps(payload).encode('utf-8')


def _write_artifact(
    settings,
    store,
    run_id: str,
    *,
    uri: str,
    artifact_type: str,
    content: bytes,
) -> None:
    path = Path(settings.shared_mount_root) / uri
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    store.save_artifact(
        ArtifactRecord(
            run_id=run_id,
            type=artifact_type,
            uri=uri,
            sha256=sha256(content).hexdigest(),
        )
    )


def _prepare_run(settings, store, engine):
    run = engine.create_run(
        RunCreateRequest(
            objective='Measure verify and report evidence prompt budgets.'
        )
    )
    Path(run.honeydew_workspace).mkdir(parents=True, exist_ok=True)
    _write_artifact(
        settings,
        store,
        run.run_id,
        uri='artifacts/job-1/metrics.json',
        artifact_type='metrics',
        content=_artifact_payload(_METRICS_ENTRIES),
    )
    _write_artifact(
        settings,
        store,
        run.run_id,
        uri='artifacts/job-1/evaluation.json',
        artifact_type='evaluation',
        content=_artifact_payload(_EVALUATION_ENTRIES),
    )
    return run


def _capture_prompts(engine, monkeypatch, run_id: str) -> dict[TurnKind, str]:
    captured: dict[TurnKind, str] = {}

    def capture_turn(**kwargs):
        captured[kwargs['expected_kind']] = kwargs['prompt']
        raise RuntimeError('prompt captured')

    monkeypatch.setattr(engine, '_run_agent_turn', capture_turn)
    with pytest.raises(RuntimeError, match='prompt captured'):
        engine._verify_results(run_id)
    with pytest.raises(RuntimeError, match='prompt captured'):
        engine._write_report(run_id)
    return captured


def test_verify_and_report_prompts_carry_only_the_content_free_digest(
    orchestrator_bundle,
    monkeypatch,
) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    run = _prepare_run(settings, store, engine)
    prompts = _capture_prompts(engine, monkeypatch, run.run_id)

    verify_prompt = prompts[TurnKind.VERIFICATION]
    report_prompt = prompts[TurnKind.FINAL_REPORT]

    for prompt in (verify_prompt, report_prompt):
        # Compacted shape: digest header plus a workspace reference, no content.
        assert 'EVIDENCE DIGEST' in prompt
        assert 'evidence-snapshot.json' in prompt
        assert _RAW_MARKER not in prompt
        assert 'metric_1999' not in prompt
        assert 'metric_2399' not in prompt

    # Each prompt embeds the exact content-free digest rendering for its phase.
    verify_evidence = engine._evidence_snapshot(
        run.run_id,
        phase=EvidencePhase.VERIFICATION,
        max_bytes=settings.evidence_file_max_bytes,
    )
    report_evidence = engine._evidence_snapshot(
        run.run_id,
        phase=EvidencePhase.REPORT,
        max_bytes=settings.evidence_file_max_bytes,
    )
    assert _RAW_MARKER not in engine._inline_evidence_digest(verify_evidence)
    assert _RAW_MARKER not in engine._inline_evidence_digest(report_evidence)
    assert engine._inline_evidence_digest(verify_evidence) in verify_prompt
    assert engine._inline_evidence_digest(report_evidence) in report_prompt

    # The workspace snapshot still holds the verbatim contents, so every digest
    # entry is resolvable by reading evidence-snapshot.json.
    snapshot_path = Path(run.honeydew_workspace) / 'evidence-snapshot.json'
    snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))
    assert snapshot_path.stat().st_size > (
        len(_artifact_payload(_METRICS_ENTRIES))
        + len(_artifact_payload(_EVALUATION_ENTRIES))
    )
    assert any(
        _RAW_MARKER in json.dumps(entry)
        for entry in snapshot['artifact_contents']
    )


def test_verify_and_report_prompt_compaction_measures_token_drop(
    orchestrator_bundle,
    monkeypatch,
) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    run = _prepare_run(settings, store, engine)
    prompts = _capture_prompts(engine, monkeypatch, run.run_id)

    for phase, kind in (
        (EvidencePhase.VERIFICATION, TurnKind.VERIFICATION),
        (EvidencePhase.REPORT, TurnKind.FINAL_REPORT),
    ):
        evidence = engine._evidence_snapshot(
            run.run_id,
            phase=phase,
            max_bytes=settings.evidence_file_max_bytes,
        )
        full_text = serialize_evidence(evidence)
        digest_text = engine._inline_evidence_digest(evidence)
        before_bytes = len(full_text)
        after_bytes = len(digest_text)
        before_tokens = estimate_tokens(full_text)
        after_tokens = estimate_tokens(digest_text)
        before_tokens_approx = max(1, before_bytes // 4)
        after_tokens_approx = max(1, after_bytes // 4)
        prompt_tokens = estimate_tokens(prompts[kind])

        # The content-free digest must be a small fraction of the snapshot it
        # replaces, and the assembled prompt must be smaller than the full
        # snapshot alone.
        assert after_bytes * 5 <= before_bytes
        assert after_tokens * 5 <= before_tokens
        assert after_tokens_approx * 5 <= before_tokens_approx
        assert prompt_tokens < before_tokens
        assert after_tokens < before_tokens

        drop_pct = 100.0 - (100.0 * after_tokens_approx / before_tokens_approx)
        print(
            f'{phase.value}: full={before_bytes}B/'
            f'{before_tokens}tok({before_tokens_approx}approx) '
            f'digest={after_bytes}B/'
            f'{after_tokens}tok({after_tokens_approx}approx) '
            f'prompt={prompt_tokens}tok '
            f'({drop_pct:.1f}% approx token drop)'
        )
