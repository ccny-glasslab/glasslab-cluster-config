#!/usr/bin/env python3
"""Seed a throwaway SQLite corpus for the ``/ui`` notebook QA.

Builds a synthetic two-page PDF with PyMuPDF under a raw root, registers a
:class:`~app.schemas.KnowledgeSource` whose ``canonical_uri`` is that file's
``file://`` URI, stores a :class:`~app.corpus_rag.contracts.RagDocumentRecord`
plus one retrievable :class:`~app.corpus_rag.contracts.RagChunkRecord` and the
:class:`~app.corpus_rag.contracts.RagSectionRecord` the cited page falls under,
and writes a JSON manifest the Playwright tests read (source id, question,
cited excerpt, page, section title, and the expected viewer query).

It also seeds one completed run with digest-verified artifacts so the
three-column notebook's file tree and text preview have real content: a
linkable ``reports/report.md`` plus ``protocol/``, ``beacon/``, and
``shared-artifacts/`` entries. Artifact files are written under the run root
(the app's shared mount root in the QA environment).

The chunk text leads with the distinctive sentence, so the chat's extractive
first-sentence citation is exactly that sentence; the manifest records the
excerpt computed with the same ``_first_sentence`` helper the chat uses, so
the test can assert the citation and the viewer URL against one source of
truth.

Run with the service interpreter (needs the app dependencies and PyMuPDF):

    python scripts/qa/seed_ui_corpus.py \
        --db <path> --raw-root <dir> --run-root <shared-mount-root> \
        --manifest <path>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from uuid import uuid4

import pymupdf

# Running this file directly puts scripts/qa on sys.path, not the service
# root; make the app package importable regardless of the caller's cwd.
SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.corpus_rag.chat import _first_sentence
from app.corpus_rag.contracts import (
    RagChunkRecord,
    RagDocumentRecord,
    RagSectionRecord,
)
from app.schemas import (
    ArtifactRecord,
    KnowledgeSource,
    RunRecord,
    RunState,
    SourceType,
    utc_now,
)
from app.storage import SqliteStore

# The cited sentence: short enough to sit on one PDF line, so PyMuPDF's
# ``search_for`` finds it verbatim for the highlight boxes.
DISTINCTIVE_SENTENCE = 'Resampling improves stability of small samples.'
# A second searchable phrase on the same page, proving the PDF carries more
# than the single cited span.
SEARCHABLE_PHRASE = (
    'Glasslab corpus QA searchable phrase: variance drift across folds.'
)
CHUNK_TEXT = f'{DISTINCTIVE_SENTENCE} {SEARCHABLE_PHRASE}'
TITLE = 'Resampling Handbook (QA)'
# The section the cited page falls under; the viewer renders it beside the page.
SECTION_TITLE = 'Resampling Methods'
QUESTION = 'resampling stability small samples'
# The cited chunk is the 0-based page index; the manifest records the 1-based
# human page number the /ui citation link and the viewer URL use. The cited
# sentence lives only on page 1, so a page-base regression (the boxes route
# searching a different physical page) cannot be masked by identical content.
PAGE_COUNT = 2
PAGE_INDEX = 0
OTHER_PAGE_TEXT = 'This second page does not contain the cited sentence.'

RUN_OBJECTIVE = 'QA run: artifact tree, verified preview, and cited source'
RUN_REPORT_REF = 'reports/report.md'
RUN_REPORT_TEXT = (
    '# QA report\n\nThe artifact tree is rendered from durable artifact '
    'records.\n'
)
RUN_ARTIFACTS = (
    (RUN_REPORT_REF, RUN_REPORT_TEXT, 'report'),
    ('shared-artifacts/evaluation-contract-proposal.json', '{}\n', 'contract'),
    ('protocol/plan.md', '# Protocol draft\n\n- bounded workload\n', 'protocol'),
    ('beacon/heartbeat.txt', 'heartbeat\n', 'beacon'),
)


def build_pdf(path: Path) -> bytes:
    """Write a two-page PDF; only page 1 carries the cited sentence/phrase."""
    document = pymupdf.open()
    first = document.new_page()
    first.insert_text((72, 100), DISTINCTIVE_SENTENCE, fontsize=12)
    first.insert_text((72, 130), SEARCHABLE_PHRASE, fontsize=12)
    second = document.new_page()
    second.insert_text((72, 100), OTHER_PAGE_TEXT, fontsize=12)
    data = document.tobytes()
    document.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def seed_run(store: SqliteStore, run_root: Path) -> dict:
    """Insert one completed run whose artifact tree the notebook can render."""
    now = utc_now()
    run_id = uuid4().hex
    store.create_run(
        RunRecord(
            run_id=run_id,
            objective=RUN_OBJECTIVE,
            state=RunState.COMPLETE,
            evaluation_contract_id='qa-contract',
            evaluation_contract_version='1',
            evaluation_contract_digest='c' * 64,
            beaker_workspace=f'runs/{run_id}/beaker-worktree',
            honeydew_workspace=f'runs/{run_id}/honeydew-worktree',
            shared_artifacts_path=f'runs/{run_id}/shared-artifacts',
            reports_path=f'runs/{run_id}/reports',
            maximum_turns=20,
            maximum_runtime_seconds=3600,
            maximum_parallel_jobs=1,
            created_at=now,
            updated_at=now,
        ),
        one_active_run=False,
    )
    refs = []
    for ref, text, artifact_type in RUN_ARTIFACTS:
        path = run_root / run_id / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode()
        path.write_bytes(data)
        store.save_artifact(
            ArtifactRecord(
                run_id=run_id,
                type=artifact_type,
                uri=f'artifact://{run_id}/{ref}',
                sha256=hashlib.sha256(data).hexdigest(),
                metadata={'path': str(path)},
            )
        )
        refs.append(ref)
    return {
        'run_id': run_id,
        'objective': RUN_OBJECTIVE,
        'report_ref': RUN_REPORT_REF,
        'report_text': RUN_REPORT_TEXT,
        'artifact_refs': refs,
    }


def seed(
    db_path: Path,
    raw_root: Path,
    run_root: Path,
    manifest_path: Path,
) -> dict:
    """Build the PDF, seed the store, and write the manifest."""
    pdf_path = raw_root / 'qa-resampling-handbook.pdf'
    data = build_pdf(pdf_path)

    store = SqliteStore(str(db_path))
    source = KnowledgeSource(
        source_type=SourceType.PAPER,
        canonical_uri=pdf_path.as_uri(),
        digest=hashlib.sha256(data).hexdigest(),
        title=TITLE,
    )
    store.save_knowledge_source(source)

    document = RagDocumentRecord(
        doc_id=f'qa-doc-{source.source_id}',
        source_id=source.source_id,
        doc_type='paper',
        title=TITLE,
        extraction_version='pymupdf-v1',
    )
    store.upsert_rag_document(document)

    chunk = RagChunkRecord(
        chunk_id=f'{source.source_id}::c0',
        source_id=source.source_id,
        doc_id=document.doc_id,
        kind='evidence_span',
        chunk_index=0,
        text=CHUNK_TEXT,
        digest=hashlib.sha256(CHUNK_TEXT.encode()).hexdigest(),
        token_count=max(1, len(CHUNK_TEXT.split())),
        page_start=PAGE_INDEX,
        page_end=PAGE_INDEX,
    )
    store.replace_rag_chunks(source.source_id, [chunk])

    section = RagSectionRecord(
        doc_id=document.doc_id,
        path='1',
        title=SECTION_TITLE,
        level=1,
        page_start=PAGE_INDEX,
        page_end=PAGE_INDEX,
    )
    store.replace_rag_sections(document.doc_id, [section])

    excerpt = _first_sentence(CHUNK_TEXT)
    assert excerpt == DISTINCTIVE_SENTENCE, (
        f'chat first-sentence excerpt drifted: {excerpt!r}'
    )
    run = seed_run(store, run_root)

    manifest = {
        'run': run,
        'source_id': source.source_id,
        'title': TITLE,
        'question': QUESTION,
        'excerpt': excerpt,
        'section_title': SECTION_TITLE,
        'page': PAGE_INDEX + 1,
        'page_count': PAGE_COUNT,
        'chunk_text': CHUNK_TEXT,
        'distinctive_sentence': DISTINCTIVE_SENTENCE,
        'searchable_phrase': SEARCHABLE_PHRASE,
        'pdf_path': str(pdf_path),
        'pdf_sha256': hashlib.sha256(data).hexdigest(),
        'db_path': str(db_path),
        'raw_root': str(raw_root),
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--raw-root', required=True, type=Path)
    parser.add_argument('--run-root', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    args = parser.parse_args()
    manifest = seed(args.db, args.raw_root, args.run_root, args.manifest)
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
