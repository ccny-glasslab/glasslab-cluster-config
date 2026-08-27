# Feed The Knowledge Corpus (Honeydew/Beaker Sources)

This runbook covers the operator flow for getting source material — a folder
of PDFs on a laptop, markdown notes, text files — into the research
orchestrator's knowledge corpus, where Honeydew's method advisory and both
agents' context retrieval can use it.

Canonical feature PR: #224. Design detail:
[`honeydew-method-advisor-2026-08.md`](../honeydew-method-advisor-2026-08.md).

## How sources reach agents

```
operator folder (local or lab)
  -> scripts/upload_knowledge_dir.py   (or single-file HTTP calls)
  -> POST /knowledge/sources/upload     (operator-token gated)
       fail-closed checks: size cap, secret path/content scan,
       born-digital PDF extraction (scanned PDFs rejected)
  -> knowledge_sources / knowledge_chunks rows + dense vectors
  -> POST /knowledge/index/rebuild      (script does this automatically)
  -> Honeydew protocol_draft / methodology_review advisories
     and Beaker context retrieval cite knowledge://<source_id>
```

Everything an agent later cites resolves to a durable record: ranked chunks
are pinned in a persisted ContextPacket (`knowledge://context:<packet_id>`)
and every advisory carries a sha256 digest plus an append-only event.

## Prerequisites

- Research orchestrator reachable (from a workstation: port-forward through
  the provisioner as described in `docs/access-topology.md`).
- Operator token when the deployment enables `require_operator_auth`
  (`X-Glasslab-Operator-Token` header; env `GLASSLAB_OPERATOR_TOKEN` is read
  by the upload script).
- The image must include the #224 changes (upload endpoint, PDF backend).
  `/health` → `knowledge_dense` reports readiness; absent key means the
  deployed image predates this feature.

## Quick start: one folder of PDFs

```bash
python services/research-orchestrator/scripts/upload_knowledge_dir.py \
    --url http://127.0.0.1:18080 \
    --dir ~/Documents/methods-pdfs \
    --source-type documentation
```

Behavior:

- walks the folder for `*.pdf`, `*.md`, `*.txt` (sorted, recursive)
- uploads each file; per-file `[ok]`/`[fail]` lines with reasons
- identical re-uploads deduplicate by content digest (same `source_id`)
- triggers `POST /knowledge/index/rebuild` at the end unless
  `--skip-rebuild`. That endpoint re-chunks every source AND re-embeds:
  chunk replacement cascades away the old vector rows, so the embed step is
  mandatory, not cosmetic

Exit code is non-zero if anything failed, so it is safe to wrap in loops/CI.

## Single-file alternatives

Upload content that lives outside the service filesystem:

```bash
curl -X POST http://127.0.0.1:18080/knowledge/sources/upload \
  -H "X-Glasslab-Operator-Token: $TOKEN" \
  -F "file=@methods-note.pdf" \
  -F "source_type=documentation" \
  -F "title=Methods note"
```

Ingest a file already on the service filesystem (must sit under an
allowlisted root):

```bash
curl -X POST http://127.0.0.1:18080/knowledge/sources \
  -H "X-Glasslab-Operator-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"source_type":"documentation","path":"/var/lib/glasslab-knowledge/note.md"}'
```

## Verify it landed

```bash
curl -s http://127.0.0.1:18080/knowledge/sources | jq '.[].canonical_uri'
curl -s http://127.0.0.1:18080/health | jq .knowledge_dense
# indexed_chunks grows after rebuild; available=true requires usable vectors
```

Advisories pick up new sources automatically: before each eligible Honeydew
phase the advisor runs an INCREMENTAL embed that vectorizes exactly the
chunks missing current-lineage vectors (matching model id, revision pin,
and dimensions), then reloads the index — so an uploaded document is
dense-retrievable on the very next advisory with no operator step. The
same incremental pass also self-heals after a revision-pin change by
re-embedding rows stored under the old lineage.

## Scanned books (OCR)

The upload endpoint stays born-digital-only so a 500-page scan can never
stall an HTTP request. For scans, extract offline first, then push the
text:

```bash
# One-time system requirement (operator side, NOT part of the service image):
#   apt install tesseract-ocr

python services/research-orchestrator/scripts/ingest_pdfs_ocr.py \
    --dir ~/books/scans --out ~/books/txt --ocr

python services/research-orchestrator/scripts/upload_knowledge_dir.py \
    --url http://127.0.0.1:18080 --dir ~/books/txt \
    --source-type documentation
```

Budget roughly 1–3 s per page on CPU (a 500-page book is tens of minutes).
`manifest.json` in the output folder records per-file status so re-runs
resume instead of re-recognizing finished books. Recognition quality bounds
retrieval quality — a poor scan yields poor embeddings regardless of the
retrieval stack.

## Correcting mistakes

- Remove a wrong source:
  `DELETE /knowledge/sources/{source_id}` (or `/by-digest/{digest}`).
- Re-uploading changed content creates a NEW source under the same
  `upload://<filename>` URI — delete the stale one explicitly.
- Deletion removes chunks and vectors with the source.

## Boundaries worth remembering

- **Scanned/image-only PDFs are rejected** (415). OCR is deliberately out of
  scope; re-export a born-digital PDF instead.
- **Size cap**: uploads larger than `knowledge_max_source_bytes` are refused
  (413). The default (2 MiB) suits papers and notes; large textbooks need a
  deliberate deployment-level raise:

  ```text
  GLASSLAB_ORCHESTRATOR_KNOWLEDGE_MAX_SOURCE_BYTES=524288000   # 500 MiB
  ```

  Set it in the orchestrator deployment env before uploading big books;
  memory during ingestion scales with the file, so raise it on a host that
  can spare the RAM.
- **Secrets never enter the index**: filename patterns, credential-content
  patterns, and long-base64 heuristics reject the whole file fail-closed.
- **Role scoping decides visibility, not labels alone**: Honeydew reads
  methodology/evaluation/verified-result classes; Beaker reads
  implementation/protocol/job-log classes. A methodology PDF tagged as an
  implementation source is invisible to advisories by design.
- **Corpus sources are global**: uploads default to unscoped +
  `run-approved`. Never set `run_scope`/`access_policy='run-private'` on
  shared material — private sources are retrievable only inside their own
  run (that boundary is regression-tested).
- **Embedding revision pinning**: if `knowledge_embedding_revision` is set,
  the loader honors it and stored vectors from other revisions are ignored
  (reported via readiness reason), not silently mixed.
