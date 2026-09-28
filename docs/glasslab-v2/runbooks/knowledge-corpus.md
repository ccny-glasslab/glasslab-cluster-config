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
curl -s -H "X-Glasslab-Operator-Token: $TOKEN" \
  http://127.0.0.1:18080/knowledge/sources | jq '.[].canonical_uri'
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

## GPU embedding

Embedding runs on the orchestrator pod CPU are slow at corpus scale (hours
for ~10k chunks). For bulk batches, embed on a cluster GPU with a bounded
Job — the verified path:

```bash
# 1. The embed-script ConfigMap is tracked in-repo (it mirrors
#    scripts/corpus_gpu_embed.py byte-for-byte; a unit test fails if the two
#    drift), so apply it with the Job:
kubectl apply -f kubeadm/glasslab-v2/jobs/corpus-embed-script-configmap.yaml

# 2. Run the Job (kubeadm/glasslab-v2/jobs/corpus-gpu-embed.yaml):
kubectl apply -f kubeadm/glasslab-v2/jobs/corpus-gpu-embed.yaml
kubectl -n glasslab-v2 logs job/corpus-gpu-embed -f

# 3. Verify parity:
#    vectors == chunks for the lineage in Postgres, then confirm the
#    orchestrator serves them on the next advisory (/health.knowledge_dense).
```

Notes: the Job pins the immutable `sha-<sha>-benchmark-gpu` workspace-runner
image tag (bump it when that image changes); it reuses the HF weights cache
already populated by the orchestrator; vectors are written with the same
model/revision/dims lineage so the orchestrator's numpy index reloads and
serves them without any service change. The orchestrator in-process CPU path
remains the small-batch fallback.

## Batch ingestion into the configured store (cluster Jobs)

The local CLI path (`fetch_corpus.py` -> `ingest_corpus.py`) defaults its
staging root to `GLASSLAB_ORCHESTRATOR_CORPUS_RAG_RAW_ROOT`, so raw PDFs land
where the orchestrator serves them from. For cluster-scale batches, run the
same code as bounded Jobs against the configured Postgres store:

```bash
kubectl apply -f kubeadm/glasslab-v2/jobs/corpus-ingest.yaml
kubectl -n glasslab-v2 logs job/corpus-ingest -f
```

`corpus-ingest.yaml` reads a manifest at
`/mnt/artifacts/research-orchestrator/rag/manifest.jsonl` and the staged PDFs
under `/mnt/artifacts/research-orchestrator/rag/raw` on the shared-artifacts
PVC, writing records to the Postgres store (never a SQLite file). The daily
`corpus-arxiv-sync` CronJob does the same for fresh preprints: it downloads
each PDF, stages it under the raw root, and records the staged `file://` URI
as the canonical URI (the original https URL is kept in record metadata). It
runs the orchestrator image, which bundles `app/` and `scripts/` in-image, so
no script ConfigMap is required. Both Jobs run as uid/gid 10001 to match the
orchestrator, and the raw root is shared with it so the chat/PDF viewer can
resolve citations.

## Making operator-uploaded sources citable in `/ui` (backfill)

The operator upload path (`POST /knowledge/sources/upload`) stores only the
extracted text plus a sha256 digest of the original bytes; it does **not**
retain the raw file. Every such source is recorded with
`canonical_uri=upload://<name>`. The `/ui` chat and PDF viewer read a different
store — the corpus-RAG tables (`orchestrator_rag_*`) — and the viewer serves
only `file://` PDFs under `corpus_rag_raw_root`. Two consequences:

- `upload://` sources are invisible to `/ui` until their retained text is
  projected into the `orchestrator_rag_*` tables.
- The PDF viewer can never open an `upload://` source: there is no raw file to
  serve and the canonical URI is not a `file:` PDF. Re-scraping LibreTexts
  (above) reproduces the **text**, not a PDF, so it does not change this.

Project the retained knowledge text into the rag store so the lexical `/ui`
chat can cite it:

```bash
kubectl apply -f kubeadm/glasslab-v2/jobs/rag-backfill.yaml
kubectl -n glasslab-v2 logs job/rag-backfill -f
```

The Job runs `scripts/corpus_rag/backfill_rag_from_knowledge.py --apply`, which
is dry-run by default and idempotent (a source that already has a
`rag_document` is skipped, so re-runs add nothing). The projection keeps each
knowledge `chunk_id` and `digest` and stamps rows with
`index_version`/`extraction_version = knowledge-backfill-v1`, and it records no
page geometry, so a backfilled citation carries no PDF link. The CLI also
accepts `--source-id <id>` (repeatable) and `--limit N` for a staged rollout.

Retrieval hydrates only the chunks its channels reference, so the `/ui` chat
cost tracks the candidate set rather than the whole store; this is what keeps a
corpus of tens of thousands of backfilled chunks responsive.

The projection is reversible: every row it writes carries the
`knowledge-backfill-v1` marker, so removing the `live-knowledge` corpus and the
marked rows returns the store to its pre-backfill state without touching
sources ingested by the PDF pipeline.

```sql
DELETE FROM orchestrator_rag_corpus_sources
 WHERE corpus_id = (SELECT corpus_id FROM orchestrator_rag_corpora
                     WHERE slug = 'live-knowledge');
DELETE FROM orchestrator_rag_chunks
 WHERE payload->>'index_version' = 'knowledge-backfill-v1';
DELETE FROM orchestrator_rag_documents
 WHERE payload->>'extraction_version' = 'knowledge-backfill-v1';
DELETE FROM orchestrator_rag_corpora WHERE slug = 'live-knowledge';
```

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

## Scraping open textbooks (LibreTexts)

For foundations coverage (undergraduate math/stats), scrape LibreTexts books
to clean markdown and upload them:

```bash
python services/research-orchestrator/scripts/scrape_libretexts.py \
    --all --out corpus-textbooks
python services/research-orchestrator/scripts/upload_knowledge_dir.py \
    --url http://127.0.0.1:18080 --dir corpus-textbooks \
    --source-type documentation --skip-rebuild
```

- The curated 8-book manifest (Calculus OpenStax, Linear Algebra Kuttler,
  Real Analysis Trench, DEs Trench, Discrete Math Davies, Probability
  Siegrist, Intro Stats OpenStax, Abstract Algebra Judson) lives in
  `services/research-orchestrator/scripts/libretexts-books.json`.
- Requires `pandoc` + `beautifulsoup4`; math is preserved as LaTeX
  (`$...$`), figures are excluded.
- `--skip-rebuild` then trigger the (long) dense rebuild separately:
  `curl -X POST -m 3600 .../knowledge/index/rebuild` with the operator
  token — re-embedding the whole corpus takes tens of minutes on CPU.
