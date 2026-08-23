# Glasslab Current Handoff

Last updated: 2026-08-13

This is the compact current-state checkpoint for switching human or coding
agents. Read `AGENTS.md` first for stable rules, architecture, vocabulary,
access paths, and live inspection commands. Read `TODO.md` for the prioritized
work queue and `docs/glasslab-v2/current/README.md` for the current docs index.

## State Layers

Three distinct layers of truth, in decreasing authority:

1. **Committed state** — the `ccny-glasslab/glasslab-cluster-config` repository
   (`testing` is the shared integration branch; `main` is production).
2. **Deployed state** — the image rolled out to the cluster, checked from the
   provisioner and recorded here only when last verified.
3. **Unverified live state** — anything not recently confirmed from `.44`; never
   assert it as current.

## Runtime And Storage

- **Runtime:** OpenCode is the selected agent runtime for Honeydew and Beaker
  (issue-98 validation path). Hermes is retained only as an explicit opt-in
  rollback backend.
  OpenCode is installed only as a rollback runtime, selected only by setting
  `GLASSLAB_ORCHESTRATOR_AGENT_RUNTIME_BACKEND` back to `opencode`.
- **Store:** PostgreSQL is the production store
  (`GLASSLAB_ORCHESTRATOR_STORE_BACKEND=postgres`). SQLite remains the local,
  test, and import-migration backend.

## Live Infrastructure Facts

The research orchestrator runs as a single pod on `node05`. Its per-run
workspaces and durable artifacts are on `glasslab-shared-artifacts`, backed by
NFS at:

```text
192.168.1.207:/volume1/backup/glasslab-v2/shared-artifacts
```

Both agent runtimes point at the exo OpenAI-compatible service at
`192.168.1.17:52415`. The cabled exo pair is `.17` and `.18`.

## Deployed State (last verified 2026-08-23)

At the last check the deployed research-orchestrator image was commit `c525861`
(#169), running with `AGENT_RUNTIME_BACKEND=opencode` (flipped for the #98
validation run) and
`STORE_BACKEND=postgres`. Re-verify before relying on it:

```bash
ssh glasslab-provisioner
sudo -n env KUBECONFIG=/home/glasslab/.kube/config \
  kubectl -n glasslab-v2 get deploy glasslab-research-orchestrator \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```

## Committed State

Merged to `main`:

- Hermes runtime for Honeydew and Beaker (#127); OpenCode retained as rollback.
- PostgreSQL orchestrator store with a SQLite import tool (#130).
- ccny GHCR image namespace cutover (#131, #132) and store migration tooling
  (#133, #134, #135).
- Hermes structured-output hardening (#142) and workflow-api live-status
  degradation (#144).

Merged to `testing` (not yet promoted to `main`):

- read-only turn inspection (#147)
- research runtime storage retention and cache cleanup (#148)
- docs consolidation (#152)

The orchestrator test suite passes 175 tests. GitHub CI is green for the
committed branch.

## Historical Research Runs

These are past runs preserved for context; they do not describe current live
state.

### Adult Income

Run `cce710ceef97441685c777c8f19c767b` reached `COMPLETE`, including final
acceptance. It was the first completed end-to-end compatibility example.

### Wine Clustering

Run `39101d9c9d3d4753bcd74e93e6106819` ended `TIMED_OUT` at turn 20.

What happened:

1. The initial matrix incorrectly expanded ten internal stability seeds into
   ten outer cluster jobs.
2. Workload calculations completed, but the evaluator rejected the results
   because `plots/clusters.png` was absent.
3. Honeydew identified the missing evidence.
4. Beaker added a deterministic PCA cluster plot using Matplotlib's
   noninteractive backend.
5. Beaker proposed a corrected matrix with one outer job and seed `17`.
6. Live deterministic preflight passed with no errors.
7. The overall run deadline expired before Honeydew could review the final
   proposal and expose execution approval.

The preserved one-job Wine proposal is the intended first use of the
terminal-checkpoint retry path (#92 / #145).

### Fashion-MNIST

The compatibility task was preflight-ready at the time but did not complete a
live run (#101).

## Inspect Live State

From a contributor workstation:

```bash
ssh glasslab-provisioner
sudo -n env KUBECONFIG=/home/glasslab/.kube/config \
  kubectl -n glasslab-v2 get pods,jobs -o wide
```

For the internal orchestrator API, create a tunnel:

```bash
ssh -L 18080:127.0.0.1:18080 glasslab-provisioner \
  'sudo -n env KUBECONFIG=/home/glasslab/.kube/config \
   kubectl -n glasslab-v2 port-forward \
   svc/glasslab-research-orchestrator 18080:8080'
```

Then:

```bash
RUN=<run-id>
curl -fsS "http://127.0.0.1:18080/runs/$RUN" | jq
curl -fsS "http://127.0.0.1:18080/runs/$RUN/events" | jq
curl -fsS "http://127.0.0.1:18080/runs/$RUN/artifacts" | jq
curl -fsS "http://127.0.0.1:18080/runs/$RUN/turns" | jq
```

Per-run files are available inside the orchestrator pod at:

```text
/mnt/artifacts/research-orchestrator/runs/<run-id>/
```

## Known Risks

- One orchestrator replica remains a scaling limitation; PostgreSQL is the
  production store but the single replica and Postgres availability are still
  single points of the research path.
- Agent turns can be slow against the shared exo model; large evidence bundles
  amplify the problem.
- Terminal checkpoint retry (#145) is merged to `testing` and deployed; a
  terminal retry child is superseded by the next retry rather than reopened.
- Hermes runtime storage does not yet have the same shared-cache treatment as
  OpenCode; only per-run cleanup applies (see
  `services/research-orchestrator/scripts/cleanup-run-storage.py`).
- A generic arbitrary-dataset run has not yet completed end to end (#98).

Update this file whenever the active deployment, current blocker, or next legal
workflow step materially changes. Keep historical detail in dated docs or run
records rather than allowing this handoff to grow indefinitely.
