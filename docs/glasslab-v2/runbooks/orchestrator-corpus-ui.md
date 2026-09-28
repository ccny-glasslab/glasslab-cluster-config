# Orchestrator Corpus UI (Read-Only)

This runbook covers the read-only corpus and reports page served by the
research orchestrator at `GET /ui/`, and the loopback proxy that makes it
usable from a browser without handing the operator token to the page. The page
is server-rendered with no JavaScript, no CDN, and a strict Content-Security
Policy, so it renders with scripting disabled.

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
| `--allow-methods` | `GET,HEAD` | Comma-separated HTTP methods to forward. |

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
- **Host-validated.** A request whose `Host` header doesn't name the loopback
  listener is rejected with `421 Misdirected Request`. This closes DNS
  rebinding, where an attacker name resolves to `127.0.0.1` but the browser
  still sends the attacker-controlled `Host`.
- **Header hygiene.** Hop-by-hop headers are dropped in both directions, and
  any client-supplied copy of the injected header is replaced with the trusted
  token, so the client can't smuggle its own value.
- **Streaming.** Upstream responses are relayed unbuffered, so
  `GET /runs/{run_id}/events/stream` (Server-Sent Events) arrives incrementally
  even while other requests are in flight.

## The three panes

The page is a single `GET /ui/` view with three panes. Selection moves through
query parameters, so the URL is the state:

| Parameter | Used by |
| --- | --- |
| `run` | Selects a run; drives the sources packet list and the document artifact list. |
| `ref` | Selects a linkable artifact for the document preview. |
| `packet` | Selects a context packet for the evidence inspector. |
| `excerpt` | Classifies a citation excerpt against the selected packet. |

Example:

```text
http://127.0.0.1:19090/ui/?run=<run-id>&ref=reports/report.md
http://127.0.0.1:19090/ui/?run=<run-id>&packet=<packet-id>&excerpt=<quoted-text>
```

1. **Sources.** The recorded runs (id, state, objective), the corpus sources
   (title, type, scope, truncated digest), and, once a run is selected, its
   context packets (packet id, agent, turn number and kind, query). Selecting a
   packet opens it in the evidence inspector.
2. **Document.** The selected run's linkable artifacts, and a digest-verified
   text preview of the chosen one. Only run-relative refs under `reports/`,
   `plots/`, `tables/`, and `shared-artifacts/` are previewable. The body is
   shown as escaped text inside a `<pre>` block, never as rendered markdown or
   HTML. Preview is capped at 2 MiB. Ranked-source URIs and filesystem paths
   are never emitted.
3. **Evidence inspector.** The selected context packet: its ranked sources
   (rank, source id, truncated digest, score) and the citation classification
   for the supplied excerpt.

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

## Read-only scope

The page and its proxy never mutate state. In particular:

- No agent turn control. Reading a run does not pause, resume, cancel, or
  retry it. The corpus chat pane is a read-only question-and-answer path, not a
  control surface; see
  [Corpus Chat And In-Browser PDF Viewer](orchestrator-corpus-ui-chat.md).
- No approves, rejections, contract promotion, or dataset or corpus changes.
- No in-page PDF rendering on this base view. A PDF that is a linkable
  artifact is previewed only as its extracted text here. The in-browser PDF
  viewer and exact-span highlighting are covered by
  [Corpus Chat And In-Browser PDF Viewer](orchestrator-corpus-ui-chat.md).
- No write methods. The proxy forwards `GET` and `HEAD` only by default.

The page reads only durable orchestrator records (runs, corpus sources,
context packets, artifact records) plus digest-verified artifact text. The
database and append-only event log remain authoritative.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `401 Unauthorized` | The operator token is missing or wrong. Confirm `GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN` is exported in the shell that started the proxy, then restart it. The token is read once at startup. |
| `421 Misdirected Request` | The request `Host` didn't name the loopback listener. Open `http://127.0.0.1:19090/ui/`, not a hostname, and don't change `--listen` without matching the URL. |
| `405 Method Not Allowed` | The proxy is read-only. It forwards `GET` and `HEAD` only unless you widen `--allow-methods` deliberately. |
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
