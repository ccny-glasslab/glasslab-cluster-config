# Glasslab Current Handoff

Last updated: 2026-09-09

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

- **Runtime:** OpenCode is the selected agent runtime for Honeydew and Beaker.
  Hermes is retained only as an explicit opt-in rollback backend, selected by
  setting `GLASSLAB_ORCHESTRATOR_AGENT_RUNTIME_BACKEND=hermes`.
- **Store:** PostgreSQL is the production store
  (`GLASSLAB_ORCHESTRATOR_STORE_BACKEND=postgres`). SQLite remains the local,
  test, and import-migration backend.

## Model Serving (exo retired — verified 2026-09-09)

The exo cluster is retired. Each Mac runs one full model locally, behind a
serializing guard. **Both servers are launchd-managed** (`KeepAlive`) via
`scripts/model-serve/install-model-serve.sh` (applied on `.18`; `.17`
migration deferred until the active rehearsal pauses).

| Host | Model | mlx port | guard port | cache |
|---|---|---|---|---|
| `192.168.1.17` | `mlx-community/Qwen3-Coder-Next-4bit` | 52416 | 52417 | `--prompt-cache-size 8` |
| `192.168.1.18` | `mlx-community/Qwen3-Next-80B-A3B-Thinking-4bit` | 52416 | 52417 | `--prompt-cache-size 8` |

- `model_guard.py` on :52417 is a single-worker serializing proxy
  (64 MB body cap, 503+retry-5s when busy). The orchestrator and rehearsal
  harness talk only to the guard.
- Servers run with `HF_HOME=/private/tmp/hf-cache`, `HF_HUB_OFFLINE=1` —
  never re-download the 42 GB weights.
- **Cold start is slow**: after a reload the first request can take ~2 min
  (weights page in from SSD); warm requests are ~10s.
- `.17` (64 GB RAM) is tight: ~48 GB wired to the Coder model, ~13 GB working
  pool. `.18` is comfortable (~70% free).

## Live Infrastructure Facts

The research orchestrator runs as a single pod on `node05`. Its per-run
workspaces and durable artifacts are on `glasslab-shared-artifacts`, backed by
NFS at:

```text
192.168.1.207:/volume1/backup/glasslab-v2/shared-artifacts
```

## Deployed State (verified 2026-09-09)

Both `glasslab-research-orchestrator` and `glasslab-workflow-api` are deployed
at commit `97d0caa` (the run-through stack: resumable rehearsal driver,
no-auto-retry turn bounds, evidence offload, matrix revision cap, matrix
rejection feedback). #382's PostgreSQL store fix is merged to `testing` but
**not yet deployed** (a rollout recreates the orchestrator pod and would kill
the active rehearsal).

Re-verify before relying on it:

```bash
ssh glasslab-provisioner
sudo -n env KUBECONFIG=/home/glasslab/.kube/config \
  kubectl -n glasslab-v2 get deploy glasslab-research-orchestrator \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```

## Committed State (merged to `testing`/`main`, 2026-09-09)

Run-through stack:

- #384 — rehearsal driver is state-driven and resumes cleanly across turn
  timeouts (paused runs resume via `resume_run()`; `RESUMABLE` exit 0).
- #385 — wall-clock/stuck-loop turn aborts are no longer auto-retried (they
  paused cleanly instead); watchdog wins the race at the turn wall.
- #386 — exo split/reload check + model context-budget measurement scripts.
- #387 — evidence snapshots are written to the agent workspace; prompts carry
  a content-free digest (context management).
- #388 — deterministic matrix-preflight revision cap (`maximum_matrix_revisions
  = 3`; the live loop burned 10 before the file was written).
- #390 — matrix-rejection feedback names the missing base_config file and
  enumerates per-requirement YAML shapes.
- #392 — evidence-URI resolver accepts the URI shapes the engine actually
  produces; `event://` resolution implemented; prompts stop inviting
  unresolvable `git://`/`contract://`.
- #393 — launchd automation for the standalone mlx servers + guards.
- #382 — PostgreSQL JSONB decode fix (stale-paused store contract) + CI
  restoration; the python CI lane is green again.

## Run-Through Status (real-model rehearsal, 2026-09-09)

A rehearsal drives the full research flow with real OpenCode + real models and
a fake cluster (`app/rehearse_research_flow.py`, in the image). It has reached
further than any prior run: protocol + contract drafted/promoted, Beaker
implementation completed, matrix proposed (rejected 10× — the loop the cap
bounds), verification has not yet run against real model output.

- A fresh rehearsal is running on `97d0caa` (started ~19:55 UTC) at the
  protocol gate. Rehearsal state is now pinned to the shared PVC: #426 adds
  `REHEARSE_ROOT=/mnt/artifacts/research-orchestrator/rehearsal` to the
  orchestrator deployment, so the checkpoint and SQLite store live on
  `glasslab-shared-artifacts` and **survive a rollout** (a Recreate still kills
  the running process, but relaunching the driver resumes from the durable
  checkpoint). Until that manifest is rolled out, the live pod still writes to
  the ephemeral `/tmp` default.
- The next unproven gate is `HONEYDEW_VERIFYING`: the #392 evidence-URI fix is
  merged but not yet deployed (same rollout constraint).

## Known Risks

- **One orchestrator replica**; the deployment is a single pod and `/tmp` is
  ephemeral — a rollout kills any in-pod rehearsal.
- **`.17` memory is the hard floor**: 64 GB with ~48 GB wired to Coder-Next
  leaves ~13 GB working pool; context growth competes with page cache. Keep
  evidence inlined out of prompts (#387) and loops bounded (#385/#388).
- **Cold start after model reload** costs ~2 min; monitor `.17`/`.18` with
  `scripts/check-exo-model-split.sh` (the `created` timestamp surfaces reloads).
- **`.17` is still manually served** (launchd migration deferred); only `.18`
  is reboot-survivable so far.
- **CI python lane is green again** (#382) but the fix is not yet deployed to
  the live orchestrator.
- Agent turns remain slow (~60s/stream); evidence compaction (#93) is the open
  lever for the verify/report stages.

Update this file whenever the active deployment, current blocker, or next legal
workflow step materially changes. Keep historical detail in dated docs or run
records rather than allowing this handoff to grow indefinitely.