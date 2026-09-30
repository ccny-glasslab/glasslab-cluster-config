# Orchestrator Corpus UI: Corpus Chat And In-Browser PDF Viewer

This runbook covers the corpus chat pane and the in-browser PDF viewer added to
the research orchestrator's read-only `/ui/` page. It supersedes the
"No live chat" and "No PDF page highlighting" caveats in
[Orchestrator Corpus UI (Read-Only)](orchestrator-corpus-ui.md); that runbook
stays authoritative for the proxy, the three panes, and the citation badges.

Canonical issues: #618 (corpus chat) and #619 (in-browser PDF viewer).

## Infra prerequisite (read this first)

The chat pane and the PDF viewer are faces over the corpus-RAG store and the
staged source PDFs. Neither works until both of the following exist. This is
the usual reason a deployment shows an empty chat or refuses to open a
document.

1. **Raw PDFs staged.** The viewer serves the original PDF bytes, and exact-span
   highlighting re-reads them on demand, so the raw files must stay on disk.
   Stage corpus raw PDFs under:

   ```text
   /mnt/artifacts/research-orchestrator/rag/raw
   ```

   That path is a subdirectory of the `glasslab-shared-artifacts` PVC, so it is
   NFS-backed and survives pod restarts. The deployment ConfigMap sets
   `GLASSLAB_ORCHESTRATOR_CORPUS_RAG_RAW_ROOT` to that path; the code default is
   the host-oriented `/tmp/glasslab-research-orchestrator/rag/raw`, so a
   deployment that relies on the code default stages nothing persistent. Change
   the setting only when the directory is mounted somewhere else.

2. **Corpus ingested into the store.** The chat retrieves chunks from the
   corpus-RAG store, it does not re-parse PDFs per question. In production the
   store is Postgres, and the corpus lives in the `orchestrator_rag_*` tables
   (`orchestrator_rag_corpora`, `orchestrator_rag_corpus_sources`,
   `orchestrator_rag_documents`, `orchestrator_rag_sections`,
   `orchestrator_rag_chunks`, `orchestrator_rag_chunk_vectors`). On a local or
   single-node run, point `GLASSLAB_ORCHESTRATOR_CORPUS_RAG_STORE_PATH` at an
   explicit on-disk store instead of using Postgres. See
   [Feed The Knowledge Corpus](knowledge-corpus.md) for the ingestion flow.

Until both of these are in place the UI degrades gracefully rather than
erroring. The chat reports, in words, that no corpus evidence is available to
answer the question, and the PDF pane offers no document to open. An empty
corpus is an expected state, not a failure.

## Prerequisites

- The read-only corpus UI proxy from
  [Orchestrator Corpus UI (Read-Only)](orchestrator-corpus-ui.md) is running on
  `127.0.0.1:19090`, and `GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN` is exported
  in the shell that started it. The proxy injects the operator token, so
  browser and `curl` traffic through it does not carry the header itself.
- The orchestrator port-forward from the same runbook is up. The proxy returns
  `502` when the upstream or the SSH forward is down.
- The deployed image includes the #618 and #619 changes. An image without them
  serves the base read-only page and no chat or PDF routes.

## Endpoints

Everything below is operator-gated at the orchestrator. Through the loopback
proxy the token is injected for you; a direct request without the token gets
`401`.

| Method and path | Purpose |
| --- | --- |
| `GET /ui/` | The whole page. With `?c=<conversation-id>` the multi-turn corpus chat replays that conversation. Selection parameters: `run`, `ref`, `packet`, `excerpt`, `c`, `source`, `page`. |
| `POST /ui/chat` | Appends a chat turn: `q` (the question) and optional `c` (conversation id). Persists the turn and 303-redirects to `GET /ui/?c=<id>#latest`. |
| `GET /ui/pdf/document.pdf?source=<source-id>` | The raw corpus PDF as a Starlette `FileResponse`. Honors HTTP Range requests and advertises `Accept-Ranges: bytes`. |
| `GET /ui/pdf/boxes?source=<source-id>&page=<n>&excerpt=<text>` | JSON highlight rectangles for `excerpt` on the 1-based `page`. `excerpt` is optional and capped at 400 characters. |
| `GET /ui/pdf/assets/{path}` | Vendored pdf.js assets under `static/pdfjs`, served with an explicit MIME map. |
| `GET /ui/pdf/assets/web/highlight.html?...` | The first-party wrapper the `/ui/` cited-source iframe actually loads: a thin dark page that fetches the document and asks `/ui/pdf/boxes` for geometry. |

The page itself still emits no script. The only JavaScript that runs is inside
the same-origin viewer iframe.

## Why the chat is POST

Each turn is a `POST /ui/chat` that persists the turn and 303-redirects to
`GET /ui/?c=<conversation-id>#latest`, which replays the conversation. The
earlier single-turn `GET /ui/?q=` form is gone.

- A `GET` that mutates (persisting a turn and calling the synthesis model) would
  re-fire on refresh, history navigation, and prefetch, duplicating turns;
  `POST` makes the append explicit per submission.
- The loopback UI proxy must forward `POST` for the chat, launch, and gate
  forms: run it with `--allow-methods GET,HEAD,POST`. Every state-changing
  request it forwards must carry a loopback `Origin`; see
  [Orchestrator Corpus UI](orchestrator-corpus-ui.md).
- The question travels in the request body rather than the URL, but a proxied
  session can log request bodies: do not paste secrets, tokens, or private
  data into the chat box.

## CSP deltas

The base page ships a strict policy built on `default-src 'none'`. Two
directives change for this feature:

| Directive | Base page | With chat and viewer | Why |
| --- | --- | --- | --- |
| `form-action` | `'none'` | `'self'` | The chat, run-launch, and gate forms submit same-origin. `'none'` blocks all form submission. |
| `frame-src` | absent | `'self'` | The PDF viewer is embedded in a same-origin iframe. With `default-src 'none'`, an unlisted frame source is blocked. |

Every other directive is unchanged: the page still has no CDN, no remote
script, and the per-response nonce is still the only way inline style is
allowed. The PDF routes carry their own tighter policies: the raw document is
served under `default-src 'none'; frame-ancestors 'self'`, and the viewer shell
may load only same-origin scripts, workers, styles, and fonts.

### CSP sign-off

The page CSP is signed off with exactly two deliberate deltas from
`default-src 'none'`:

| Directive | Value | Why `'none'` breaks it |
| --- | --- | --- |
| `form-action` | `'self'` | `'none'` blocks the zero-JS chat, launch, and gate forms, so none can ever submit. |
| `frame-src` | `'self'` | `'none'` (or an unlisted source under `default-src 'none'`) blocks the same-origin cited-source PDF iframe, so selecting a citation shows a blank pane. |

Everything else stays default-deny. There is deliberately no `script-src`:
the page itself runs no JavaScript, and the only script that runs is inside
the first-party same-origin viewer iframe, which carries its own policy. The
guard test `test_ui_csp_sign_off_directives` pins these four facts, so a
future edit that loosens the base policy (or strips `form-action`/`frame-src`)
fails review.

## The PDF viewer

- **Vendored pdf.js 6.3.289**, served same-origin from `/ui/pdf/assets/**`. The
  assets ship with the service image; there is no CDN and no external fetch.
- **The document endpoint** is `GET /ui/pdf/document.pdf?source=<source-id>`. It
  is served by a Starlette `FileResponse`, which honors HTTP Range requests.
  Range support is what lets the viewer seek and render a page without pulling
  the whole file first.
- **The cited-source wrapper** is a thin first-party page,
  `/ui/pdf/assets/web/highlight.html`, that the `/ui/` iframe loads. It imports
  the vendored pdf.js module, fetches the document from the same-origin
  endpoint, and asks `/ui/pdf/boxes` for highlight geometry. The upstream
  `viewer.html` shell is **not** served: its relative refs cannot resolve
  through this service's route shape, so no `/ui/pdf/viewer.html` route exists.
- **Module worker and `.mjs` MIME.** pdf.js loads its worker as an ES module
  (`pdf.worker.mjs`). The asset route serves `.mjs` as `text/javascript`, or the
  browser refuses the module worker and the viewer stays blank.
- **No `GZipMiddleware`.** No compression middleware wraps these routes.
  Compression strips `Content-Length` and re-chunks the body, which makes byte
  ranges unreliable; seeking and page rendering then break. The PDF route
  returns uncompressed bytes.
- **The iframe is intentionally not sandboxed.** The viewer needs same-origin
  module workers, blob URLs, and same-origin fetches, and a `sandbox` attribute
  breaks all three. The content is first-party, same-origin, already constrained
  by the page CSP, and reachable only through the loopback token-injecting
  proxy.

The document route confines reads to the configured raw root: a source must
resolve to a `file:` URI with a `.pdf` suffix, must not be a symlink, and must
resolve inside `GLASSLAB_ORCHESTRATOR_CORPUS_RAG_RAW_ROOT`. Every rejection is
an opaque `404`, so the response never reveals whether a path exists.

## The chat pane

The chat pane is a persistent, multi-turn conversation over indexed corpus
chunks: each `POST /ui/chat` appends a turn, and the prior turns feed the
synthesis prompt (the last few exchanges, under a token budget) so follow-up
questions keep their context. A conversation is keyed by `conversation_id` and
stored durably, so `GET /ui/?c=<id>` replays it. Retrieval is
configurable: `lexical` (the default) needs no embedding backend, `dense` uses
the vector channel only, and `hybrid` fuses lexical and dense with RRF.
Synthesis is extractive by default: the answer quotes the top retrieved
passages and renders each one as a citation. Dense and hybrid additionally
require the dense index to be built first (see
[Dense and hybrid retrieval](#dense-and-hybrid-retrieval) below), and both
degrade to lexical when the embedding backend or the index is unavailable.
Selecting a citation opens the PDF viewer at the cited source and drives the
exact-span highlight.

## Grounded LLM answers

With `GLASSLAB_ORCHESTRATOR_RAG_LLM_ENABLED=true` the chat asks the hosted
OpenCode Go gateway to synthesize a grounded answer instead of quoting raw
passages. The deployment sets this on, so the operator `/ui` chat generates
answers from the retrieved evidence.

- **Endpoint and model.** OpenAI-compatible
  `POST https://opencode.ai/zen/go/v1/chat/completions` with model
  `deepseek-v4.1-flash`. Override the endpoint with
  `GLASSLAB_ORCHESTRATOR_RAG_LLM_BASE_URL` and the model with
  `GLASSLAB_ORCHESTRATOR_RAG_LLM_MODEL`.
- **Key.** Read at call time from the OpenCode auth file at
  `GLASSLAB_ORCHESTRATOR_OPENCODE_AUTH_JSON_PATH`
  (default `/etc/opencode-auth/auth.json`), entry `opencode-go` -> `key`. The
  key stays in the mounted secret; it never appears in the ConfigMap, config,
  logs, or the request URL. The same secret already authenticates the agent
  runtime.
- **Fallback.** Every failure — the file or key is missing, the model is down,
  a non-2xx response, a malformed body, or citations that do not resolve to a
  retrieved hit — silently falls back to the extractive answer, so the page
  never fails on a model error.
- **Disable.** Set `GLASSLAB_ORCHESTRATOR_RAG_LLM_ENABLED=false` and roll the
  deployment to return to purely extractive answers. Startup never depends on
  the provider: with the flag off the chat is built without one.

## Dense and hybrid retrieval

Dense retrieval reads `orchestrator_rag_chunk_vectors`; the ~38k corpus chunks
projected by `scripts/corpus_rag/backfill_rag_from_knowledge.py` carry no
vectors until the embed step runs. The embed step is idempotent: it embeds only
evidence-span chunks that lack a vector for the active model lineage, so
re-running it is a no-op.

Embeddings are produced in-process by the Snowflake arctic-embed provider
(`sentence-transformers`) from the model weights cached at
`/mnt/artifacts/research-orchestrator/hf-cache`; the deployment pins the
resolved HuggingFace revision (`GLASSLAB_ORCHESTRATOR_KNOWLEDGE_EMBEDDING_REVISION`)
so stored vectors match what the orchestrator queries. The orchestrator image
ships the CPU torch runtime, so the embed Job runs on CPU and requests no GPU.
A GPU variant of the same Job is available for full-corpus batches (see
[GPU embed Job](#gpu-embed-job) below).

Run the embed step as a Job:

```bash
kubectl apply -f kubeadm/glasslab-v2/jobs/corpus-rag-embed.yaml
kubectl -n glasslab-v2 logs job/corpus-rag-embed -f
```

Or run the CLI directly (dry run by default; add `--apply` to write):

```bash
python services/research-orchestrator/scripts/corpus_rag/embed_rag_chunks.py
python services/research-orchestrator/scripts/corpus_rag/embed_rag_chunks.py --apply
```

Then flip the chat mode by setting
`GLASSLAB_ORCHESTRATOR_UI_CHAT_RETRIEVAL_MODE` in the orchestrator ConfigMap
(`hybrid` is the recommended first step) and rolling the deployment. The mode
defaults to `lexical`, so leaving the key unset preserves today's behavior. A
deployment that selects `dense`/`hybrid` before the vectors exist degrades to
lexical rather than returning no evidence.

### GPU embed Job

At full corpus scale the CPU embed Job takes hours. `corpus-rag-embed-gpu.yaml`
is the same `embed_rag_chunks.py --apply --embedding settings --vector-backend
auto` run on the GPU image (`glasslab-research-orchestrator-gpu`): the
orchestrator code with a CUDA-enabled torch. It requests one `nvidia.com/gpu`,
uses `runtimeClassName: nvidia`, and keeps the CPU Job's Postgres label and
store/lineage env, so it writes the identical vectors.

```bash
kubectl apply -f kubeadm/glasslab-v2/jobs/corpus-rag-embed-gpu.yaml
kubectl -n glasslab-v2 logs job/corpus-rag-embed-gpu -f
```

Pin the image to a commit whose GPU image CI has published: the workflow builds
`glasslab-research-orchestrator-gpu:<commit-sha>` alongside the CPU image, so
the tag moves with each merge. Like the CPU Job the embed step is idempotent —
it embeds only chunks missing a vector for the active lineage, so a re-run adds
nothing and computes only the missing vectors.

## Exact-span highlighting

Highlight coordinates are computed on demand, not stored:

```text
GET /ui/pdf/boxes?source=<source-id>&page=<page-number>&excerpt=<quoted-text>
```

The route opens the raw PDF for `source`, runs PyMuPDF `search_for` for the
excerpt on the requested page, and returns the matching rectangles in a
JSON object:

```json
{"page": 12, "page_size": [595.0, 842.0], "boxes": [[x0, y0, x1, y1]]}
```

`page` is **1-based** (the human page number) at every HTTP boundary: the
`/ui/` citation link emits `page_start + 1`, the viewer requests
`getPage(page)`, and this route indexes `document[page - 1]`. The rendered
page and the boxes page are therefore the same physical page. The boxes are
returned in PDF user-space coordinates (origin bottom-left) because that is
what the pdf.js viewport consumes; the route flips PyMuPDF's top-left rects
so the highlight is not drawn mirrored.

No bounding box is persisted at ingestion. Coordinates go stale the moment a
PDF is re-extracted or replaced, so deriving them live from the staged bytes
keeps the store small and always matches the file being served. If the excerpt
does not match on that page, or it spans pages, `boxes` is empty and the viewer
shows the page without a highlight. A `page` below `1` or above the page
count, and an excerpt longer than 400 characters, return `400`. This is why the
raw PDFs must remain staged (see the prerequisite above): without them there is
nothing to search.

## Config keys

All orchestrator settings use the `GLASSLAB_ORCHESTRATOR_` prefix unless noted.

| Setting | Default | Purpose |
| --- | --- | --- |
| `GLASSLAB_ORCHESTRATOR_CORPUS_RAG_RAW_ROOT` | `/tmp/glasslab-research-orchestrator/rag/raw` (code); the deployment ConfigMap overrides it to `/mnt/artifacts/research-orchestrator/rag/raw` | Directory holding the raw corpus PDFs. |
| `GLASSLAB_ORCHESTRATOR_CORPUS_RAG_STORE_PATH` | unset | Optional explicit corpus-RAG store path. Leave it unset in production, where the Postgres `orchestrator_rag_*` tables hold the corpus; set it only to relocate the store to an explicit on-disk path. |
| `GLASSLAB_ORCHESTRATOR_UI_CHAT_ENABLED` | `true` | Gates the chat pane. When `false`, `/ui/` still renders but carries no Ask the corpus section. |
| `GLASSLAB_ORCHESTRATOR_UI_CHAT_RETRIEVAL_MODE` | `lexical` | Chat retrieval mode: `lexical`, `dense`, or `hybrid`. Non-lexical modes require the dense index to be built (`scripts/corpus_rag/embed_rag_chunks.py`) and degrade to lexical when the backend or index is unavailable. |
| `GLASSLAB_ORCHESTRATOR_UI_PDF_ENABLED` | `true` | Gates the viewer routes. When `false`, no `/ui/pdf/**` route is registered, so every such path returns `404`. |
| `GLASSLAB_ORCHESTRATOR_RAG_LLM_ENABLED` | `false` | Opts the `/ui` chat into grounded LLM synthesis through the hosted OpenCode Go gateway. The deployment sets it to `true`; the key is read from the OpenCode auth file, not config. Every failure falls back to the extractive answer, so the page never depends on the model. |
| `GLASSLAB_ORCHESTRATOR_RAG_LLM_BASE_URL` | `https://opencode.ai/zen/go/v1` | OpenAI-compatible base URL for chat synthesis; the deployment leaves it at the default. |
| `GLASSLAB_ORCHESTRATOR_RAG_LLM_MODEL` | `deepseek-v4.1-flash` | Model name sent to the synthesis endpoint. |
| `GLASSLAB_ORCHESTRATOR_RAG_LLM_TIMEOUT_SECONDS` | `60.0` | Per-request timeout for chat synthesis; a timeout falls back to the extractive answer. |

None of these carry secret values in the manifest. The LLM API key belongs in
the deployment Secret, not in the ConfigMap or a tracked file.

## Verification

With the proxy running and the operator token exported:

```bash
# Page renders (200) and carries the chat form.
curl -s -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:19090/ui/'

# Chat turn: POST persists it and 303-redirects to ?c=…#latest (expect 303).
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  --data-urlencode 'q=resampling stability' \
  'http://127.0.0.1:19090/ui/chat'

# Full document fetch (no Range): expect 200.
curl -s -o /dev/null -w '%{http_code}\n' \
  'http://127.0.0.1:19090/ui/pdf/document.pdf?source=<source-id>'

# Range fetch: expect 206 and a Content-Range header.
curl -s -D - -o /dev/null -H 'Range: bytes=0-1023' \
  'http://127.0.0.1:19090/ui/pdf/document.pdf?source=<source-id>'

# Exact-span boxes: expect a JSON object; boxes is empty when nothing matches.
curl -s \
  'http://127.0.0.1:19090/ui/pdf/boxes?source=<source-id>&page=12&excerpt=<quoted-text>' \
  | jq
```

Open the viewer at `http://127.0.0.1:19090/ui/`, select a corpus source, and
the PDF pane loads the document; a citation from a chat answer opens the same
viewer at the cited page with the excerpt highlighted.

A direct `curl` against `http://127.0.0.1:18080/ui/pdf/document.pdf?source=...`
without the operator token returns `401`, which confirms the route is
operator-gated and that the proxy is the intended path.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Chat reports no corpus evidence | The store holds no ingested corpus, or the raw PDFs are not staged. Complete the infra prerequisite, then reload. |
| Chat cites a source but the PDF pane is empty | The source is an operator `upload://` row: uploads discard the raw bytes, so the viewer can never serve a PDF for them. Only `file://` PDFs (staged under the raw root) render. See [Feed The Knowledge Corpus](knowledge-corpus.md#making-operator-uploaded-sources-citable-in-ui-backfill). |
| `405 Method Not Allowed` on submit | The proxy isn't forwarding `POST`. Run it with `--allow-methods GET,HEAD,POST`. |
| Blank PDF pane | The viewer assets are missing from the image, or `.mjs` is served with the wrong MIME type and the module worker is rejected. Check the browser console and the asset route's `Content-Type`. |
| Viewer loads the first page but will not seek | The PDF route is compressed or not returning ranges. Confirm no compression middleware wraps the PDF routes and that a `Range` request returns `206`. |
| Page renders but no highlight | The excerpt did not match on that page, or it spans pages. Confirm the raw PDF is staged at the configured raw root and that the page number is right. |
| `400` from `/ui/pdf/boxes` | `page` is below `1` or past the page count, or `excerpt` is longer than 400 characters. Shorten the excerpt or use a valid 1-based page. |
| `404` from `/ui/pdf/document.pdf` | The source is unknown, or its URI is not a `file:` PDF, is a symlink, is missing, or resolves outside the raw root. All of these collapse to one opaque `404`. |
| `401 Unauthorized` | The proxy is not injecting a valid operator token. Confirm the token variable is set in the proxy's shell and restart the proxy. |
| `502 Bad Gateway` | The SSH port-forward or the orchestrator service is down. Restore both, then reload. |
| Long question fails | The question is capped at 2000 characters before synthesis. Shorten it. |

## Read-only scope (updated)

The chat feature itself does not loosen the read-only boundary. Specifically:

- The chat is a read path: it retrieves corpus chunks and renders text. It
  does not mutate run state, and the grounded-answer call is a stateless
  synthesis request that falls back to the extractive answer on any failure.
- No run control from the chat. Reading or chatting does not pause, resume,
  cancel, or retry any run.
- The chat form is `GET`, so it writes nothing.

The page also carries a separate, opt-in operator control surface (launch a
run, pause/resume/cancel, approve or reject a gate). The proxy forwards it
only when run with `--allow-methods GET,HEAD,POST`, and it guards every
state-changing request with a loopback `Origin`; see
[Operator controls](orchestrator-corpus-ui.md#operator-controls).

The page reads durable orchestrator records and the corpus-RAG store, plus
digest-verified artifact text and the staged raw PDFs. The database and
append-only event log remain authoritative.

## Related

- [Orchestrator Corpus UI (Read-Only)](orchestrator-corpus-ui.md)
- [Feed The Knowledge Corpus](knowledge-corpus.md)
- [Contributor Access](../../contributor-access.md)
- [Research Orchestrator Command Surface](../../research-orchestrator-command-surface.md)
