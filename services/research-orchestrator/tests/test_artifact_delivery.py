"""Verified artifact delivery and the analysis notebook projection.

Covers the VerifiedArtifactReader (rejects path escapes and digest
mismatches), the run artifact bundle (only verified, successful-job artifacts
plus a digest-carrying manifest), and build_analysis_notebook embedding
verified metrics and tables into a runnable notebook.
"""

from __future__ import annotations

from hashlib import sha256
import io
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from app.analysis_notebook import build_analysis_notebook
from app.artifact_delivery import (
    ArtifactDeliveryError,
    VerifiedArtifactReader,
    build_report_bundle,
    build_run_artifact_bundle,
)
from app.schemas import ArtifactRecord, JobStatus


def _artifact(
    root: Path,
    *,
    run_id: str,
    job_id: str | None,
    relative: str,
    artifact_type: str,
    content: bytes,
) -> ArtifactRecord:
    # Writes a real file and returns the matching ArtifactRecord, so the
    # reader/bundle code paths see consistent on-disk state and digests.
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return ArtifactRecord(
        run_id=run_id,
        job_id=job_id,
        type=artifact_type,
        uri=relative,
        sha256=sha256(content).hexdigest(),
    )


def test_verified_reader_rejects_escape_and_digest_mismatch(tmp_path) -> None:
    outside = tmp_path.parent / 'outside-artifact.txt'
    outside.write_text('not available')
    reader = VerifiedArtifactReader(str(tmp_path))

    with pytest.raises(ArtifactDeliveryError, match='unavailable'):
        reader.resolve(
            ArtifactRecord(
                run_id='run-1',
                type='report',
                uri=str(outside),
                sha256=sha256(outside.read_bytes()).hexdigest(),
            )
        )

    artifact = _artifact(
        tmp_path,
        run_id='run-1',
        job_id=None,
        relative='reports/report.md',
        artifact_type='report',
        content=b'original',
    )
    (tmp_path / 'reports/report.md').write_bytes(b'changed')
    with pytest.raises(ArtifactDeliveryError, match='digest mismatch'):
        reader.resolve(artifact)


def test_bundle_contains_successful_verified_results_and_manifest(tmp_path) -> None:
    run_id = 'run-1'
    artifacts = [
        _artifact(
            tmp_path,
            run_id=run_id,
            job_id='job-ok',
            relative='artifacts/result/metrics.json',
            artifact_type='metrics.json',
            content=b'{"accuracy": 0.9}\n',
        ),
        _artifact(
            tmp_path,
            run_id=run_id,
            job_id='job-ok',
            relative='artifacts/result/source.zip',
            artifact_type='source.zip',
            content=b'frozen source',
        ),
        _artifact(
            tmp_path,
            run_id=run_id,
            job_id='job-failed',
            relative='artifacts/failed/runner.log',
            artifact_type='logs/runner.log',
            content=b'failed output',
        ),
    ]
    jobs = [
        SimpleNamespace(job_id='job-ok', status=JobStatus.SUCCEEDED),
        SimpleNamespace(job_id='job-failed', status=JobStatus.FAILED),
    ]

    bundle = build_run_artifact_bundle(
        run_id=run_id,
        artifacts=artifacts,
        jobs=jobs,  # type: ignore[arg-type]
        shared_mount_root=str(tmp_path),
        maximum_bytes=1024 * 1024,
    )

    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        assert 'jobs/job-ok/metrics.json' in archive.namelist()
        assert not any('job-failed' in name for name in archive.namelist())
        assert not any(name.endswith('source.zip') for name in archive.namelist())
        manifest = json.loads(archive.read('artifact-manifest.json'))
    assert manifest['run_id'] == run_id
    assert manifest['artifacts'][0]['sha256'] == artifacts[0].sha256


def test_reader_resolves_workflow_api_artifacts_bucket_prefix(tmp_path) -> None:
    content = b'{"score": 0.75}\n'
    path = tmp_path / 'external-run-1' / 'metrics.json'
    path.parent.mkdir()
    path.write_bytes(content)
    artifact = ArtifactRecord(
        run_id='run-1',
        job_id='job-1',
        type='metrics.json',
        uri='artifacts/external-run-1/metrics.json',
        sha256=sha256(content).hexdigest(),
    )

    assert VerifiedArtifactReader(str(tmp_path)).resolve(artifact) == path


def test_analysis_notebook_embeds_verified_metrics_and_tables(tmp_path) -> None:
    metrics = _artifact(
        tmp_path,
        run_id='run-1',
        job_id='job-1',
        relative='artifacts/result/metrics.json',
        artifact_type='metrics.json',
        content=b'{"accuracy": 0.91, "passed": true}\n',
    )
    table = _artifact(
        tmp_path,
        run_id='run-1',
        job_id='job-1',
        relative='artifacts/result/tables/metrics.csv',
        artifact_type='tables/metrics.csv',
        content=b'model,accuracy\nlinear,0.85\ntree,0.91\n',
    )

    notebook = json.loads(
        build_analysis_notebook(
            run_id='run-1',
            job_id='job-1',
            artifacts=[metrics, table],
            shared_mount_root=str(tmp_path),
        )
    )

    assert notebook['nbformat'] == 4
    assert notebook['metadata']['glasslab']['kind'] == 'verified-result-analysis'
    source = ''.join(notebook['cells'][2]['source'])
    assert 'plot.barh' in source
    assert 'frame.plot' in source
    embedded = ''.join(notebook['cells'][1]['source'])
    assert '0.91' in embedded
    assert 'linear,0.85' in embedded


def test_report_bundle_produces_pdf_and_docx_with_deterministic_names() -> None:
    body = '# Glasslab Report\n\nAccuracy 0.91.\n'
    bundle = build_report_bundle(
        run_id='run-abc123',
        report_body=body,
        timestamp='20260904T120000Z',
    )

    assert bundle.pdf_filename == 'glasslab-run-abc123-20260904T120000Z.pdf'
    assert bundle.docx_filename == 'glasslab-run-abc123-20260904T120000Z.docx'
    assert bundle.pdf.startswith(b'%PDF-')
    assert b'Accuracy 0.91' in bundle.pdf
    assert bundle.docx.startswith(b'PK')
    with zipfile.ZipFile(io.BytesIO(bundle.docx)) as archive:
        assert 'word/document.xml' in archive.namelist()
        assert 'Accuracy 0.91' in archive.read('word/document.xml').decode()


def test_report_bundle_rejects_empty_body() -> None:
    with pytest.raises(ArtifactDeliveryError, match='empty'):
        build_report_bundle(
            run_id='run-1',
            report_body='   \n\t',
            timestamp='20260904T120000Z',
        )
