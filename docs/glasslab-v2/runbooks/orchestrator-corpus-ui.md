# Orchestrator Corpus UI

This runbook covers the corpus and reports page served by the research
orchestrator at `GET /ui/`, and the loopback proxy that makes it usable from a
browser without handing the operator token to the page. The page is
server-rendered with no JavaScript, no CDN, and a strict Content-Security
Policy, so it renders with scripting disabled. The base page is read-only; an
opt-in control surface (launch a run, pause/resume/cancel, approve or reject a
gate) is covered under [Operator controls](#operator-controls).

Canonical feature PR: #592.

## Prerequisites

- The orchestrator is reachable through the provisioner port-forward. The
  `glasslab-provisioner` SSH alias already carries
  `LocalForward 18080 127.0.0.1:18080`; see
  [Contributor Access](../../contributor-access.md). The manual equivalent is:

  ```bash
  ssh -L 18080:127.0.0.1:18080 glasslab-provisioner
  ```

- The operator token is exported in the environment as
  `GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN`. Read it from the deployment
  secret. Do not put the value in a file, shell history, issue, or chat.

- Python 3 on the workstation. The proxy uses only the standard library, so
  there is nothing to install.

## Start the proxy

The proxy listens on `127.0.0.1:19090` and forwards to
`http://127.0.0.1:18080`, injecting `X-Glasslab-Operator-Token` read from the
environment:

```bash
python3 scripts/glasslab-orchestrator-ui-proxy.py
```

Leave it running and open:

```text
http://127.0.0.1:19090/ui/
```

On startup the proxy prints the listen address, the upstream origin, and the
header name to stderr. The token value is never printed.

### Flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--listen` | `127.0.0.1:19090` | Loopback `host:port` to bind. |
| `--upstream` | `http://127.0.0.1:18080` | Loopback orchestrator origin. Must be a bare origin with no path, query, or fragment. |
| `--token-env` | `GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN` | Name of the environment variable that holds the operator token. |
| `--header` | `X-Glasslab-Operator-Token` | Header injected on every upstream request. |
| `--allow-methods` | `GET,HEAD` | Comma-separated HTTP methods to forward. Add `POST` to enable the [Operator controls](#operator-controls). |
| `--allow-paths` | *empty* | Comma-separated request-path prefixes to forward. Empty forwards every path (the base page listener). Use `--allow-paths /ui/pdf/` for the viewer-origin listener; any other path gets `403` before the operator token is injected. See [Viewer origin](#viewer-origin). |

The proxy is deliberately constrained:

- **Loopback only.** The listen address and the upstream must both resolve to
  loopback addresses. There is no flag to bind `0.0.0.0` or to reach a
  non-loopback upstream.
- **Token from the environment only.** The token is never accepted as a
  command-line argument, since process arguments are visible to other local
  users through `ps`. It is never logged or written to disk. The process
  refuses to start when the variable is unset or empty.
- **Read-only by default.** Only the allowlisted methods are forwarded.
  Anything else gets `405 Method Not Allowed` with an `Allow` header and never
  reaches the orchestrator.
- **Origin-guarded writes.** When `POST` is allowlisted, every state-changing
  request must be same-origin per Fetch Metadata `Sec-Fetch-Site`
  (`same-origin` or user-initiated `none`); when that header is absent, a
  loopback `Origin`/`Referer` for the listen port is required instead.
  Anything else gets `403 Forbidden` before the operator token is injected.
  This closes the cross-site form-post hole that widening the method allowlist
  would otherwise open. (Fetch Metadata is the primary signal because the
  page's `Referrer-Policy: no-referrer` reduces a same-origin form POST's
  `Origin` to the opaque `null`.)
- **Host-validated.** A request whose `Host` header doesn't name the loopback
  listener is rejected with `421 Misdirected Request`. This closes DNS
  rebinding, where an attacker name resolves to `127.0.0.1` but the browser
  still sends the attacker-controlled `Host`.
- **Header hygiene.** Hop-by-hop headers are dropped in both directions, and
  any client-supplied copy of the injected header is replaced with the trusted
  token, so the client can't smuggle its own value.
- **Path-scoped (optional).** `--allow-paths` restricts the listener to a
  comma-separated prefix allowlist; every other request path is rejected with
  `403` before the operator token is injected. The base page listener keeps
  the empty default (all paths); the viewer listener on the second origin is
  scoped to `/ui/pdf/`, so a viewer-side script cannot reach `/runs`; see
  [Viewer origin](#viewer-origin).
- **Streaming.** Upstream responses are relayed unbuffered, so
  `GET /runs/{run_id}/events/stream` (Server-Sent Events) arrives incrementally
  even while other requests are in flight.

## Viewer origin

The cited-source PDF viewer is served from a second loopback origin so a
script running inside the viewer cannot reach the operator read API: the
single listener injects the operator token on every path, so an unsandboxed
viewer sharing that origin could `fetch('/runs')`. Both listeners point at the
same upstream; only the request-path allowlist differs.

```bash
# Terminal 1: the operator page on 19090 (every path).
python3 scripts/glasslab-orchestrator-ui-proxy.py

# Terminal 2: the viewer origin on 19091, /ui/pdf/ only.
python3 scripts/glasslab-orchestrator-ui-proxy.py \
  --listen 127.0.0.1:19091 --allow-paths /ui/pdf/
```

Open `http://127.0.0.1:19090/ui/`. The deployment ConfigMap pins
`GLASSLAB_ORCHESTRATOR_UI_ORIGIN=http://127.0.0.1:19090` and
`GLASSLAB_ORCHESTRATOR_UI_PDF_VIEWER_ORIGIN=http://127.0.0.1:19091`, so the
page emits an absolute viewer iframe, the page CSP admits that origin in
`frame-src`, and the viewer CSP admits the page origin in `frame-ancestors`.

A request to the viewer listener outside `/ui/pdf/` (for example `/runs`) is
answered `403` and never reaches the orchestrator, which is what contains a
viewer-side script. With both settings unset the page falls back to the
pre-#620 relative single-origin iframe and an empty path allowlist.

## The three panes

The page is a single `GET /ui/` view with three panes. Selection moves through
query parameters, so the URL is the state:

| Parameter | Used by |
| --- | --- |
| `run` | Selects a run; drives the sources packet list, the Viewer column's Turns tab, and the viewer artifact tree. |
| `ref` | Selects a linkable artifact for the viewer preview (opens the Viewer tab). |
| `packet` | Selects a context packet for the evidence inspector. |
| `excerpt` | Classifies a citation excerpt against the selected packet. |
| `c` | Selects a multi-turn corpus-chat conversation to replay. |
| `source` / `page` | Selects the cited corpus source (and 1-based page) for the PDF viewer (opens the Viewer tab). |

Example:

```text
http://127.0.0.1:19090/ui/?run=<run-id>&ref=reports/report.md
http://127.0.0.1:19090/ui/?run=<run-id>&packet=<packet-id>&excerpt=<quoted-text>
```

1. **Sources.** The recorded runs (id, state, objective), the corpus sources
   (title, type, scope, truncated digest), and, once a run is selected, its
   context packets (packet id, agent, turn number and kind, query). Selecting a
   packet opens it in the evidence inspector.
2. **Ask the corpus.** The persistent, multi-turn conversation over indexed
   corpus chunks: the composer posts a turn to `/ui/chat`, and the replayed
   conversation renders each answer with inline citation markers. See
   [Corpus Chat And In-Browser PDF Viewer](orchestrator-corpus-ui-chat.md).
3. **Viewer.** A two-tab surface over the selection. **Turns** lists the
   selected run's redacted agent-turn summaries (agent, status, the
   structured-output kind and summary, start/end timestamps, and the error
   when set) in storage order, oldest first; it renders read-only
   `TurnSummary` fields only, never raw tool-call transcripts. **Viewer** holds
   the existing viewer body: the cited source's PDF viewer iframe (same-origin,
   or the configured viewer origin when origin isolation is enabled; see
   [Viewer origin](#viewer-origin)), or its stored extracted text when no PDF
   is servable, or the selected run's artifact tree with the digest-verified
   text preview of the chosen file.
   Selecting a run opens Turns; selecting a source or a file opens Viewer.
   The strip is the same pure-CSS `:checked` radio group as the Sources tabs,
   so the page stays zero-JS.

## Citation badges

The badge is computed by matching the excerpt against the exact text the
packet supplied to the agent. It is not read from the stored
`ranked_sources[].verified` flag, which is a tautology recorded at build time
and is never an independent verification.

| Badge | Meaning |
| --- | --- |
| `✓ exact` | The deterministic verbatim matcher confirms the whole excerpt appears in the matched context block. |
| `≈ fuzzy` | Only the normalized alphanumeric prefix of the excerpt (first 36 characters) matched the block. |
| `✗ unverified` | The excerpt didn't locate a supplied context block at all. |

The inspector labels the located block by its 1-based ordinal and shows the
source id, digest, and score, then the matched block text.

## Operator controls

The base page is read-only, but the same page carries an opt-in control
surface when the proxy forwards `POST`. It is gated by the same operator token
and uses ordinary same-origin forms (no script, no inline style):

| Form | Action | Route |
| --- | --- | --- |
| Start a research run | Starts a run from an objective (optional contract id/version) | `POST /ui/runs` |
| Pause / Resume / Cancel | Pause, resume, or cancel the selected run | `POST /ui/runs/{run_id}/control` |
| Approve / Reject | Decide a pending human gate on the selected run | `POST /ui/actions/{action_id}/decide` |

Run the proxy with `--allow-methods GET,HEAD,POST` to enable them; with the
default `GET,HEAD` the forms are still rendered but their submission is
answered `405`. Each controller calls the same engine methods as the JSON API
(`POST /runs`, `POST /runs/{run_id}/{pause,resume,cancel}`,
`POST /actions/{action_id}/{approve,reject}`), and a validation failure
renders an escaped error page rather than a JSON body. A gate that still needs
Honeydew's sign-off renders as "awaiting Honeydew" with no decision form.

## Read-only scope

Everything else on the page never mutates state:

- No agent-turn control beyond pause/resume/cancel. Reading a run does not
  retry it, the Viewer column's Turns tab renders only redacted turn
  summaries, and the corpus chat pane remains a read-only question-and-answer
  path, not a control surface; see
  [Corpus Chat And In-Browser PDF Viewer](orchestrator-corpus-ui-chat.md).
- No contract promotion, and no dataset or corpus changes from the page.
- No in-page PDF rendering on this base view. A PDF that is a linkable
  artifact is previewed only as its extracted text here. The in-browser PDF
  viewer and exact-span highlighting are covered by
  [Corpus Chat And In-Browser PDF Viewer](orchestrator-corpus-ui-chat.md).

The page reads only durable orchestrator records (runs, actions, corpus
sources, context packets, artifact records) plus digest-verified artifact
text. The database and append-only event log remain authoritative.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `401 Unauthorized` | The operator token is missing or wrong. Confirm `GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN` is exported in the shell that started the proxy, then restart it. The token is read once at startup. |
| `421 Misdirected Request` | The request `Host` didn't name the loopback listener. Open `http://127.0.0.1:19090/ui/`, not a hostname, and don't change `--listen` without matching the URL. |
| `403 Forbidden` from the viewer origin | The 19091 listener was started with an `--allow-paths` value that doesn't cover the requested path. Run it with `--allow-paths /ui/pdf/`; see [Viewer origin](#viewer-origin). |
| `405 Method Not Allowed` | The proxy isn't forwarding the method. Run it with `--allow-methods GET,HEAD,POST` to enable the operator controls; the base page is read-only by default. |
| `403 Forbidden` on a control submit | A state-changing request didn't carry a loopback `Origin`/`Referer` for the listen port. Open the page at `http://127.0.0.1:19090/ui/` and submit from there; a cross-site form or a tool that omits `Origin` is refused. |
| `502 Bad Gateway` | The proxy reached its listen port but couldn't reach the upstream. The SSH port-forward or the orchestrator service is down. |
| Blank page or connection refused | The proxy process or the SSH session (with its `LocalForward`) isn't running. Start both, then reload. |
| `Document unavailable` | The ref is invalid, outside the linkable directories, has no artifact record, failed digest verification, or exceeds the 2 MiB preview cap. |
| Empty packet list | No run is selected yet, or the selected run has no recorded context packets. |

## Maximal-safe fallback: per-object signed links

If you don't hold the operator token, or you only need one object rather than
the whole read surface, use the signed links that reports already carry to
Discord. Each link opens at `http://127.0.0.1:18080/links/<token>` and
authorizes exactly one report, artifact, or context packet. It is not
operator-gated; the signed, expiring token is the entire authorization, and it
grants one logical object. That is the safer option when you can't or
shouldn't run the token-injecting proxy.

Signed links still need the same SSH port-forward, since the link host is
`127.0.0.1`. See
[Inspecting Signed Research Links](../../contributor-access.md) for the
forward details, and
[Feed The Knowledge Corpus](knowledge-corpus.md) for how sources reach
agents.

## Related

- [Contributor Access](../../contributor-access.md)
- [Research Orchestrator Command Surface](../../research-orchestrator-command-surface.md)
- [Feed The Knowledge Corpus](knowledge-corpus.md)
- [Drive A Real Run](drive-a-real-run.md)
