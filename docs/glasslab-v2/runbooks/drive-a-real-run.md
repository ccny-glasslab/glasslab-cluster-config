# Drive A Real Research Run

This runbook is for operating a **real, non-rehearsal Glasslab research run**
end to end: starting it, driving the four human gates, interpreting the failures
that actually show up against real models and a real cluster, and deciding when
to resume versus when to start a fresh run.

Read [`../../research-orchestrator.md`](../../research-orchestrator.md) for the
architecture and [`../../research-orchestrator-command-surface.md`](../../research-orchestrator-command-surface.md)
for the concise operator surface. This file adds the operational judgment that
is not visible from source.

Everything below is grounded in repo code/docs with a `file:line` reference.
Claims that could **not** be verified from the checked-out repo are collected in
[Unverified Claims](#unverified-claims) at the end; do not treat them as
authoritative without checking live.

## 1. What A Real Run Is

A real run drives the same bounded pipeline as the rehearsal harness, but every
external boundary is live.

| Boundary | Rehearsal harness | Real run |
| --- | --- | --- |
| Cluster execution | `FakeClusterExecutor`, never submits | `WorkflowApiClusterExecutor` -> `workflow-api` -> Kubernetes Jobs |
| Run state store | `SqliteStore` | PostgreSQL (`GLASSLAB_ORCHESTRATOR_STORE_BACKEND=postgres`) |
| Objective | hard-coded synthetic objective | operator-supplied objective or imported task bundle |
| Human gates | auto-approved by the driver | real Approve / Reject by a human |
| Agent runtime | real OpenCode + real models | real OpenCode + real models |

Grounding: the harness docstring calls itself "the full research flow against
REAL agent models with FAKE cluster" and imports `FakeClusterExecutor`
(`services/research-orchestrator/app/rehearse_research_flow.py:1-16`, `:30`); it
sets `cluster_execution_mode='fake'` (`:129`), builds a `SqliteStore` and a
`FakeClusterExecutor` (`:166-167`), and creates a synthetic objective
(`:632-640`). The driver consumes every gate automatically and only stops on a
terminal state, a human-resolution pause, or a segment boundary
(`:583-601`, `:775-840`).

**The fake path cannot catch wire-contract or shape defects.** Issue #491 is the
proof: the orchestrator sent a top-level `resources` field that `workflow-api`
forbids, so every real submission was rejected with HTTP 422 and no Kubernetes
Job was ever created - and the rehearsal could not see it because it never talks
to `workflow-api` (issue #491; fix comment at
`services/research-orchestrator/app/cluster.py:270-281`). The harness docstring
makes the same point for model-output shape defects: scripted mocks "cannot
catch the failure classes that only appear with real model output"
(`rehearse_research_flow.py:4-9`). A passing rehearsal is necessary, not
sufficient.

## 2. Entry Points

### Discord (the human front door)

Only `/task-start` is registered for starting a run. It accepts two forms:

```text
/task-start objective:<research objective>
/task-start archive:<ZIP> objective:<optional narrower objective>
```

- The objective-only form is accepted when no archive is attached; it returns
  immediately and creates the run in the background
  (`services/research-orchestrator/app/discord_controls.py:999-1047`).
- The archive form compiles, preflights, and starts a task bundle from a ZIP
  containing `problem.md` and an evaluator rubric; it defers first because the
  import takes 40-90s (`discord_controls.py:1048-1082`). See
  [`../../research-orchestrator-task-bundle-guide.md`](../../research-orchestrator-task-bundle-guide.md).

Data registration:

```text
/dataset-upload dataset:<file> name:<short-name> role:<role> contains_labels:<bool>
/dataset-url url:<https-url> name:<short-name> role:<role> contains_labels:<bool> expected_sha256:<optional>
```

Both return an immutable `glasslab-dataset://<sha256>` reference
(`discord_controls.py:726-783`). Put that reference in `problem.md` or the
objective; never embed a large dataset in the task ZIP.

The other live controls are `/research-cancel`, `/research-pause`,
`/research-resume`, `/research-artifacts`, `/research-turns`,
`/research-status`, `/research-list`, `/research-question`, `/research-promote`,
and `/packet` (`discord_controls.py:679-910`). Approve and Reject are Discord
buttons, not slash commands.

### `/research-start` and `/benchmark-start` are RETIRED

Neither name is registered. The registration block contains only `task-start`,
`research-cancel`, the dynamic `research-pause`/`research-resume`, the dataset
commands, and the read/report commands (`discord_controls.py:679-910`), and the
tests assert the retired names are absent
(`services/research-orchestrator/tests/test_discord_and_opencode.py:415-419`).

The previously stale references now defer to `/task-start`:

- `docs/research-orchestrator.md` (the Discord section and the worked example).
- `docs/research-orchestrator-discord-operator-guide.md` (`Start A Run`).
- `AGENTS.md` (Discord Workflow) and `kubeadm/glasslab-v2/research-orchestrator/README.md`.

Use `/task-start` for both objective-only and archive runs.

### HTTP (internal automation / recovery)

`POST /runs` with `{"objective": "..."}` creates an objective-only run
(`objective` has `min_length=10`; `services/research-orchestrator/app/schemas.py:983-1012`).
The task-bundle and dataset import endpoints are in the next section.

## 3. HTTP Surface (Internal-Only)

The service is `ClusterIP` only. Tunnel it through the provisioner from a
contributor workstation:

```bash
ssh -L 18080:127.0.0.1:18080 glasslab-provisioner \
  'sudo -n env KUBECONFIG=/home/glasslab/.kube/config \
   kubectl -n glasslab-v2 port-forward \
   svc/glasslab-research-orchestrator 18080:8080'
```

(`docs/research-orchestrator-command-surface.md:284-290`;
Service name/namespace/port in
`kubeadm/glasslab-v2/research-orchestrator/30-service.yaml`.)

Every route except `/health` and `/ready` requires the operator token in the
header `X-Glasslab-Operator-Token`
(`services/research-orchestrator/app/main.py:447-471`, specifically the alias at
`:450`). The token config field is `operator_api_token` (`config.py:276`) and
the env var is `GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN` (prefix at
`config.py:35`). Missing token config yields 503; a wrong token yields 401
(`main.py:456-471`). `/internal/agent-tools/retrieve-evidence` instead uses the
separate tool header `X-Glasslab-Tool-Token` (`main.py:990-999`).

```bash
TOKEN=<operator-token>
BASE=http://127.0.0.1:18080
H="X-Glasslab-Operator-Token: $TOKEN"
RUN=<run-id>
```

| Method | Path | Use |
| --- | --- | --- |
| GET | `/runs` | list runs (`main.py:1133`) |
| GET | `/runs/{run_id}` | run record / current state (`main.py:1141`) |
| GET | `/runs/{run_id}/events` | authoritative event log, `?after_sequence=` (`main.py:1151`) |
| GET | `/runs/{run_id}/turns` | redacted per-turn history, `?limit=` capped at 100 (`main.py:1187`) |
| GET | `/runs/{run_id}/artifacts` | artifact registry (`main.py:1171`) |
| GET | `/runs/{run_id}/events/stream` | SSE event stream (`main.py:1281`) |
| POST | `/runs` | create objective-only run (`main.py:587`) |
| POST | `/runs/{run_id}/resume` | resume a transiently paused run (`main.py:1221`) |
| POST | `/runs/{run_id}/cancel` | cancel run + active jobs (`main.py:1231`) |
| POST | `/runs/{run_id}/pause` | pause a run (`main.py:1211`) |
| POST | `/actions/{action_id}/approve` | approve a pending gate action (`main.py:1251`) |
| POST | `/actions/{action_id}/reject` | reject a pending gate action (`main.py:1266`) |
| POST | `/task-bundles/import` | multipart `archive` (`main.py:608`) |
| POST | `/datasets/import` | multipart `dataset`, `name`, `role`, `contains_labels`, `uploaded_by` (`main.py:640`) |

Approve body: `{"reviewer": "<name>", "reason": "<optional>"}`
(`ApprovalRequest`, `schemas.py:1023-1027`). Reject body:
`{"reviewer": "<name>", "reason": "<required>"}` (`RejectionRequest`,
`schemas.py:1030-1034`), then re-poll the run. Use
`/runs/{run_id}/events` as the source of truth for every state change.

## 4. The Four Human Gates

Each gate is a run state that waits for a specific pending action type. The
mapping is defined once for the rehearsal driver
(`rehearse_research_flow.py:203-208`) and mirrors the human-wait states in
`services/research-orchestrator/app/state_machine.py:17-22`.

| Run state waiting | Action type | What approval authorizes |
| --- | --- | --- |
| `AWAITING_PROTOCOL_APPROVAL` | `approve_protocol` | accept the protocol and its evaluation-contract proposal |
| `AWAITING_CONTRACT_PROMOTION` | `propose_evaluation_contract` | promote the sealed evaluation-contract candidate |
| `AWAITING_EXECUTION_APPROVAL` | `submit_experiment_matrix` | submit the bounded cluster experiment matrix |
| `AWAITING_FINAL_ACCEPTANCE` | `accept_final_report` | accept the final report and complete the run |

Action labels and the buttons that render them are in
`discord_controls.py:70-73` and
`services/research-orchestrator/app/discord_adapter.py:37-40`. The engine's
gate handling and the follow-on transitions are in
`services/research-orchestrator/app/engine.py:3254-3262`. Final acceptance
transitions the run to `COMPLETE` (`state_machine.py:174-181`).

Find the pending action ID for a run:

```bash
curl -fsS -H "$H" "$BASE/runs/$RUN/events" \
  | jq '[.events[] | select(.event_type | test("action"))] | .[-5:]'
```

## 5. Driving Semantics

These are the behaviors that are easy to misread while watching a live run.

### 5.1 A slow approve/resume call can time out client-side and still take effect

The approve, reject, resume, and pause handlers are synchronous `def` handlers
that call the engine inline (`main.py:1211-1229`, `main.py:1251-1279`). An
approval can synchronously drive the next agent turns, so the HTTP response can
exceed a client or proxy timeout while the server keeps working. **A client-side
timeout is not evidence of failure.** Do not retry blindly; re-poll:

```bash
curl -fsS -H "$H" "$BASE/runs/$RUN" | jq '{state, resume_state, turn_number}'
curl -fsS -H "$H" "$BASE/runs/$RUN/events" | jq '.events[-10:]'
```

Confirm the action actually moved (`approval_status`) and look for the resulting
`run.state_changed` / `run.resumed` event before acting again.

### 5.2 `POST /runs/{id}/resume` is for transient turn failures

`resume_run` refuses unless the run is `PAUSED` with a recorded `resume_state`
(`engine.py:6348-6349`). On resume it rotates a session left attached to a failed
turn (`engine.py:6350`, `:6384-6457`), transitions back to the recorded state,
re-seeds agent context, emits `run.resumed`, and runs recovery
(`engine.py:6352-6382`). Use it for the three transient turn aborts - the 3600s
wall clock, the 250-step budget, and the doom-loop guard - because the engine
rotates the session and the retry usually completes. The engine also auto-retries
retryable failures up to `agent_turn_max_retries` (default 2,
`config.py:155`; `engine.py:1682-1695`) before it pauses, so a paused run has
already exhausted the automatic retries.

### 5.3 Never auto-resume a `methodology.human_resolution_requested` pause

When methodology review exceeds the automatic revision limit
(`maximum_methodology_revisions`, default 2, `config.py:259`), the engine emits
`methodology.human_resolution_requested` and pauses the run
(`engine.py:5391-5407`). This pause is **not** transient: resuming re-enters the
same non-converging loop and burns the turn budget until the run hits
`maximum_turns` and goes `TIMED_OUT` (`engine.py:757-768`).

The rehearsal harness encodes exactly this rule: it detects a human-resolution
pause by looking for a `human_resolution_requested` event after the most recent
`run.resumed`, returns `BLOCKED`, and deliberately leaves the run paused
(`rehearse_research_flow.py:471-498`, `:720-749`). Mirror that behavior manually:

- Check the pause cause before resuming:

  ```bash
  curl -fsS -H "$H" "$BASE/runs/$RUN/events" \
    | jq '.events[-20:] | map(select(.event_type=="run.paused" or (.event_type|test("human_resolution"))))'
  ```

- If the pause reason is "Methodology review exceeded the automatic revision
  limit. Human resolution is required." (`engine.py:5402-5406`), leave it paused.
  Fix the root cause (usually a contract/profile or spec contradiction), then
  start a **fresh run** rather than resuming.

### 5.4 A recurring 409 on resume is a bug, not a transient

`WorkflowError` maps to HTTP 409 (`main.py:487-499`), and `resume_run` raises
`WorkflowError('run is not resumable')` for a non-paused run
(`engine.py:6349`). A 409 that recurs on **every** resume - with the run bouncing
`PAUSED -> target -> PAUSED` and the same `run.paused` reason each time - is a
defect with no deterministic recovery. Issue #490 is the canonical example: the
installed-contract binding path raised
"installed contract remains incompatible with the protocol" on every resume.
That path now fails closed once, before binding, with a
`methodology.resource_authority_conflict` event and an actionable reason telling
the operator to replace the stale contract or change the task profile and start a
new run (`engine.py:4020-4061`). If you still see a recurring resume 409, stop
resuming and open an issue; do not keep spending turns.

## 6. Observed Failure Taxonomy

| Symptom / log line | Where | Transient? | Operator response |
| --- | --- | --- | --- |
| `OpenCode turn aborted after 6 identical terminal tool calls` | doom-loop watchdog, `services/research-orchestrator/app/opencode_runtime.py:1164-1167`; limit 6 at `config.py:136` | No (non-retryable, `engine.py:109`) | Resume once. The engine rotates the session, emits `agent.doom_loop_detected`, and injects a corrective instruction (`engine.py:960-972`, `:1000-1014`). If it recurs, the prompt/tool is the problem - change it or start fresh. |
| `OpenCode turn exceeded the step budget of 250 steps (...)` | step watchdog, `opencode_runtime.py:1176-1180`; limit 250 at `config.py:142` | No (non-retryable, `engine.py:110-113`) | Resume. Engine emits `agent.turn_step_budget_exceeded` and injects a "stop exploring" correction (`engine.py:973-984`, `:1015-1026`). |
| `OpenCode turn exceeded the hard wall-clock limit of 3600 seconds` | wall watchdog, `opencode_runtime.py:1408-1410` | No (non-retryable, `engine.py:103-108`) | Resume; the fresh session retries. The tracked configmap (`kubeadm/glasslab-v2/research-orchestrator/10-configmap.yaml:52`) and the code default (`config.py:147`) are both 3600s, and `tests/test_configmap_parity.py` fails if the configmap ever narrows the code default. A cluster deployed before that parity fix still enforces the old 1800s, so confirm the live configmap before quoting the number. |
| `Server error '500 Internal Server Error' for url '.../session/.../message'` | OpenCode message POST, `opencode_runtime.py:941-946` | Usually transient (`httpx.HTTPError` is retryable, `engine.py:184-193`) | The engine auto-retries twice with a fresh session. If a soft pause results, resume. If it recurs on every turn, treat it as stale worktree deps (next row). |
| `Cannot find module '@opencode-ai/plugin'` and session 500 on every turn | OpenCode-native log (see [Section 7](#7-where-to-look-while-debugging)); imported by the generated tool at `opencode_runtime.py:445` | No | Stale per-run `.opencode` dependencies (#482). Do **not** keep resuming; every turn 500s and burns turns to `TIMED_OUT`. Start a **fresh run** (fresh worktrees avoid #482). |
| Provider / connect errors returned by the model endpoint | decoded at `opencode_runtime.py:182-194`; classified `provider` | Usually transient (`engine.py:116-118`, `:184-193`) | Let the engine retry; resume if it paused. Persisting errors point at the exo endpoint, not the orchestrator. |
| `methodology.human_resolution_requested` + `run.paused` "Methodology review exceeded the automatic revision limit. Human resolution is required." | `engine.py:5391-5407` | No | Do **not** resume ([Section 5.3](#53-never-auto-resume-a-methodologyhuman_resolution_requested-pause)). Fix the cause or start fresh. |
| `methodology.resource_authority_conflict` with "Promoted contracts are immutable and are never silently superseded..." | #490 fail-closed path, `engine.py:4041-4061` | No | The run fails closed. Replace the stale promoted contract or change the task runtime profile, then start a new run. |

### 6.1 Distinguishing doom loop, step budget, and wall clock

All three abort a single turn and pause the run, but they mean different things:

- **Doom loop** (`repeated_tool_loop`): the model repeated the same terminal tool
  call 6 times byte-identically. The corrective prompt tells it not to repeat the
  call; a fresh session usually recovers.
- **Step budget** (`step_budget_exceeded`): the model kept taking *different*
  steps for 250 loop steps without returning a result. The corrective prompt
  tells it to stop exploring and return what it has.
- **Wall clock** (`turn_timeout`): the turn exceeded 3600s of wall time. On the
  Thinking model this is often legitimate long reasoning; a fresh session with
  the checkpoint usually finishes.

## 7. Where To Look While Debugging

### Per-run tree

Workspace root is `GLASSLAB_ORCHESTRATOR_WORKSPACE_ROOT`, set to
`/mnt/artifacts/research-orchestrator/runs` in the deployment
(`kubeadm/glasslab-v2/research-orchestrator/10-configmap.yaml:11`; code default
`config.py:45`). The layout is
(`docs/research-orchestrator.md:153-163`):

```text
/mnt/artifacts/research-orchestrator/runs/<run-id>/
  protocol/
  beaker-worktree/
  honeydew-worktree/
  shared-artifacts/
  reports/
  events/
  runtime/beaker/
  runtime/honeydew/
```

It is an NFS-backed PVC, so read it **through the orchestrator pod**, not from
the provisioner's local filesystem:

```bash
kubectl -n glasslab-v2 exec deploy/glasslab-research-orchestrator -- \
  ls -la /mnt/artifacts/research-orchestrator/runs/<run-id>/
```

### Per-agent OpenCode runtime and logs

Each agent's runtime root is `runs/<run-id>/runtime/<agent>/` and holds
`config/`, `data/`, `state/`, and `home/` (`opencode_runtime.py:575-591`).

- The orchestrator captures the OpenCode subprocess stdout/stderr at
  `runtime/<agent>/opencode.log` (`opencode_runtime.py:675`).
- The OpenCode-native session database is
  `runtime/<agent>/data/opencode/opencode.db`; its `message` and `part` tables
  hold the session transcript (`services/research-orchestrator/app/runtime_replay.py:37`,
  `:100-119`). Query `part` for per-tool `state.status` and errors.

Path ambiguity: the operator notes and issue #482's live evidence point at
`runtime/<agent>/data/opencode/log/opencode.log`, while the repo comment says
`XDG_STATE_HOME` holds OpenCode logs/history and `XDG_DATA_HOME` holds session
auth (`config.py:161-169`; `opencode_runtime.py:578`, `:584`, `:587`, `:689`).
Check both `data/` and `state/` for the native log; this is flagged below.

The `message=loop step=N` line referenced by operator lore was **not found
anywhere in the repo** - only a "loop step" comment exists (`config.py:137`). Use
the step-budget abort reason (Section 6) and the `part` table as the durable
signals instead.

### Orchestrator log

```bash
kubectl -n glasslab-v2 logs deploy/glasslab-research-orchestrator
```

Since #480, `map_error` logs the full traceback via `logger.exception` before
returning a generic 500, so an HTTP 500 now leaves a real cause in the
orchestrator log (`main.py:500-506`).

### Authoritative records over HTTP

`GET /runs/{run_id}/events` is the authoritative timeline (including `run.paused`
reasons), `GET /runs/{run_id}/turns` is the redacted per-turn view, and
`GET /runs/{run_id}/artifacts` lists registered outputs
(`docs/research-orchestrator-command-surface.md:281-306`).

## 8. Verification / Definition Of Done

A real run is done only when all of the following hold.

1. **Real Kubernetes Jobs exist** in `glasslab-v2` (not the fake executor):

   ```bash
   ssh glasslab-provisioner
   sudo -n env KUBECONFIG=/home/glasslab/.kube/config \
     kubectl -n glasslab-v2 get jobs,pods -o wide
   ```

   Real submission goes through `workflow-api` to Jobs; #491 shows the failure
   mode where the wire contract breaks and `kubectl get jobs` reports "No
   resources found" while the run still advances (issue #491).

2. **The run reaches `COMPLETE`** through final acceptance
   (`state_machine.py:174-181`), with the terminal `accept_final_report` action
   approved.

3. **The report bundle exists** under `runs/<run-id>/reports/`:
   `report.md` plus `glasslab-<prefix>-<timestamp>.pdf` and `.docx`
   (`engine.py:6248-6296`). The filename stem is `glasslab-` + the **first 12
   characters** of the run ID + the UTC timestamp
   (`services/research-orchestrator/app/report_bundle.py:30-35`, `:178-179`).

4. **Evaluator/integrity records are present and the workload did not author
   them.** The immutable evaluator owns `evaluation.json`, `integrity_pass`, and
   `rubric_score` (`docs/research-orchestrator-command-surface.md:98-101`).

## 9. Safety Rules

- **Do not deploy or roll out services while a run is progressing.** The
  orchestrator is a single-replica `Recreate` Deployment
  (`kubeadm/glasslab-v2/research-orchestrator/20-deployment.yaml:7`, `:12-13`),
  so a rollout terminates the pod and kills the in-pod OpenCode agent runtimes
  and any active turn. The rehearsal root is pinned to the shared PVC precisely
  because a `Recreate` rollout would otherwise destroy in-progress work
  (`20-deployment.yaml:86-91`, issue #426). Wait for a terminal state before
  rolling out.
- **Preserve run directories as evidence.** `protocol/`, `reports/`,
  `shared-artifacts/`, and `events/` are durable and never cleaned; only
  worktree/runtime scratch is eligible for retention cleanup after a terminal
  state plus the retention window. Do not delete run directories to force a
  retry.
- **Never echo tokens or DSNs.** `X-Glasslab-Operator-Token` /
  `GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN`, the Postgres DSN
  (`GLASSLAB_ORCHESTRATOR_STORE_POSTGRES_DSN`), and the workflow-api token are
  secrets. Keep them out of prompts, logs, issues, and commits.
- **When a run is wedged, prefer a fresh run over reviving it.** A fresh run
  creates fresh worktrees, which avoids the stale `.opencode` dependency failure
  (#482). The frozen protocol and checkpoint artifacts make a new run an
  intentional continuation, not a loss.

## 10. Known Gaps (Reference, Do Not Re-Litigate)

- **#473** - rehearsal snapshot re-creation over the read-only review surface
  (`0444`/`0555`) raised `PermissionError`; fixed by clearing read-only bits
  bottom-up before removal (`rehearse_research_flow.py:306-336`). Rehearsal-only.
- **#482** - a per-run worktree with stale `.opencode` dependencies returns
  500 on every `/session/.../message`, so the run retries/rotates and burns
  `maximum_turns` to `TIMED_OUT`. Prefer a fresh run.
- **#490** - an installed-contract binding that cannot accommodate the compiled
  task profile produced an unresolvable resume 409 loop; now fails closed
  (`engine.py:4020-4061`).
- **#491** - the orchestrator sent a forbidden top-level `resources` field to
  `workflow-api`, so every real submission was rejected with 422 and no Job was
  created. The canonical rehearsal-vs-real gap.
- **#492** - the spec-fields audit
  (`docs/research-orchestrator-spec-fields.md`): the same field specified in
  multiple places (task profile, contract `resource_constraints`, matrix
  `resources`) can disagree; several defects were only discoverable by running a
  real job.
- **Rehearsal-vs-real list** - the harness docstring enumerates the defect
  classes a scripted-mock smoke test cannot catch: contract id reuse, structured
  envelope drift, per-turn model routing, evaluator-type derivation
  (`rehearse_research_flow.py:4-9`).

## Unverified Claims

These are used above because they match operator experience and issue evidence,
but they could **not** be confirmed from the checked-out repo. Verify live before
relying on them.

1. **Report filename prefix width.** The task brief and operator notes say
   `glasslab-<runid8>-<ts>`, but the code uses `run_id[:12]`
   (`report_bundle.py:30-35`). This runbook documents the code (12). Confirm
   against a real bundle.
2. **OpenCode-native log path.** The brief and issue #482's live evidence say
   `runtime/<agent>/data/opencode/log/opencode.log`; the repo comment says
   OpenCode logs/history live under `XDG_STATE_HOME`
   (`runtime/<agent>/state/`, `config.py:161-169`,
   `opencode_runtime.py:578-587`). The orchestrator also writes its own
   subprocess log at `runtime/<agent>/opencode.log`
   (`opencode_runtime.py:675`). The exact native path is unconfirmed.
3. **`message=loop step=N`.** No such string exists in the repo (only a "loop
   step" comment at `config.py:137`). Its exact location/format is unverified.
4. **"Approve/resume can time out client-side (>60s) but still take effect."**
   Partially grounded: the handlers are synchronous and call the engine inline
   (`main.py:1211-1229`, `:1251-1279`), so long work blocks the response. The
   specific `>60s` threshold and the "still takes effect" guarantee are operator
   observations, not asserted in code.
5. **Live wall-clock value.** 3600 seconds is the tracked configmap value
   (`kubeadm/glasslab-v2/research-orchestrator/10-configmap.yaml:52`) and the
   code default (`config.py:147`), guarded against narrowing by
   `services/research-orchestrator/tests/test_configmap_parity.py`. A cluster
   deployed before that parity fix still enforces 1800 seconds, so confirm the
   live configmap before quoting the number.
6. **Exact live error strings for #482/#491.** The Node error
   (`Cannot find module '@opencode-ai/plugin'`), the httpx 500 text, and the 422
   submission rejection are taken from the GitHub issue evidence, not from source
   literals. They may vary by OpenCode/httpx version.
7. **`part` table schema stability.** The replay reader treats `message` and
   `part` as JSON `data` columns (`runtime_replay.py:100-119`), but that is the
   OpenCode-version-specific schema; it is not guaranteed across OpenCode
   upgrades.
