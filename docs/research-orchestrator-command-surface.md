# Research Orchestrator Command Surface

Last verified: 2026-08-06

This is the concise operator and contributor reference for the Honeydew/Beaker
research workflow. The database and append-only event log are authoritative.
Discord is the normal human interface and a projection of that state.

## Discord Commands

Commands are guild-scoped and restricted to the configured Glasslab channel
and approval role or explicit administrator allowlist.

| Command | Where | Effect |
| --- | --- | --- |
| `/research-start objective:<text>` | Main Glasslab channel | Starts a question-driven run. Honeydew drafts the protocol and evaluation contract proposal. |
| `/task-start archive:<zip> [objective:<text>]` | Main Glasslab channel | Compiles an arbitrary task archive, performs preflight, and starts the run only when required inputs are ready. |
| `/benchmark-start archive:<zip> [objective:<text>]` | Main Glasslab channel | Compatibility alias for `/task-start`; do not build new integrations around this name. |
| `/dataset-upload dataset:<file> name:<name> [role:<role>] [contains_labels:<bool>]` | Main Glasslab channel | Stores a file immutably and returns a checksum-addressed `glasslab-dataset://` reference. |
| `/research-artifacts [run_id:<id>] [include_source:<bool>]` | Run thread, or main channel with `run_id` | Downloads a digest-verified ZIP of the latest run-level artifacts and successful-job outputs. |
| `/research-turns [run_id:<id>] [limit:<int>]` | Run thread, or main channel with `run_id` | Shows the run's most recent redacted agent turns (default 5, max 20) with agent identity, status, and timestamps. |
| `/research-pause [run_id:<id>] [reason:<text>]` | Run thread, or main channel with `run_id` | Aborts an active model turn, preserves state, and records where to resume. |
| `/research-resume [run_id:<id>] [reason:<text>]` | Run thread, or main channel with `run_id` | Restores a paused run to its prior state and restarts workflow recovery. |
| `/research-cancel [run_id:<id>] [reason:<text>]` | Run thread, or main channel with `run_id` | Cancels the run, aborts active Hermes turns, requests cancellation of active jobs, and records the Discord actor and reason. |

Inside a run thread, pause, resume, and cancel resolve the run from the thread
and do not require an ID.

Discord does not currently expose list or status slash commands. Status is
shown by the run thread's editable status message.

## Intended End-To-End Run

For a task ZIP, begin in the main configured Glasslab channel:

```text
/task-start archive:<attach ZIP> objective:<optional narrower objective>
```

The bot creates one public thread for the run. Continue from that thread:

1. Honeydew drafts `program.md` and a logical evaluation-contract proposal.
2. Read the protocol approval brief and use its Approve or Reject button.
3. Beaker writes `implementation-plan.md`, the workload, candidate config, and
   a normalized experiment matrix.
4. Deterministic preflight runs before Honeydew reviews the implementation.
   Rejected proposals return to Beaker with concrete feedback.
5. Read the execution approval brief. It must state job count, variants,
   seeds, resources, required artifacts, contract, and authorization scope.
6. Approve to submit the bounded matrix through `workflow-api`. Agent turns are
   idle while Kubernetes jobs run.
7. Beaker analyzes authoritative job and artifact records. Honeydew verifies
   the important claims against evaluator output and the approved protocol.
8. Honeydew writes `report.md`; use the final acceptance control to complete
   the run.
9. Use `/research-artifacts` to download the verified result bundle.

Approve and Reject are message buttons, not slash commands. An approval only
authorizes the described action; it is not evidence that execution succeeded.

For a question without an archive, use `/research-start`. Honeydew still begins
with the protocol and evaluation contract. For local data, run
`/dataset-upload` first and put the returned `glasslab-dataset://<sha256>`
reference in `problem.md` or the objective.

## Failure And Recovery Behavior

- A deterministic preflight failure rejects the matrix before cluster work and
  sends the exact errors back to Beaker.
- A Kubernetes job failure is recorded as experimental evidence and normally
  sends the workflow to Beaker analysis, followed by Honeydew verification.
- If verification finds missing or invalid evidence, a fresh bounded revision
  budget begins and Beaker receives the failure details.
- `/research-pause` aborts an active model turn but preserves the worktree and
  recovery checkpoint. `/research-resume` starts a fresh OpenCode session from
  that checkpoint.
- `FAILED`, `CANCELLED`, and `TIMED_OUT` are terminal. They cannot currently be
  resumed. `POST /runs/{run_id}/retry` creates a fresh child from the parent's
  verified checkpoint; when an earlier retry child is itself terminal, the
  next retry supersedes it.
- The run thread must receive a persisted follow-up for an execution or
  interaction failure; an ephemeral Discord error is not sufficient.

The workload emits metrics and evidence; the immutable evaluator owns
`evaluation.json`, `integrity_pass`, and `rubric_score`. Matrix seeds create
independent jobs. Do not copy an internal stability-seed list into the outer
matrix when one job already executes that list.

## Result Artifacts

Use `/research-artifacts` inside a run thread to download its verified result
bundle. By default the bundle contains the latest protocol, report, analysis
notebook, metrics, evaluation output, tables, manifests, and logs associated
with successful jobs. Failed-job files and duplicate superseded run-level
artifacts are excluded from the default delivery.

Set `include_source:true` to include frozen source and task ZIP files. The
command fails closed on path traversal, symlinks, digest mismatches, and the
configured Discord bundle-size ceiling. Every ZIP contains
`artifact-manifest.json` with the original URI, digest, job ID, and archive
path for each delivered file.

After a successful job, the orchestrator also derives `analysis.ipynb` from
digest-verified `metrics.json` and `tables/*.csv` files. The notebook embeds
those inputs and supplies generic pandas/matplotlib inspection and plotting
cells. It is marked as a non-authoritative analysis surface; the immutable
evaluator output remains the scientific evidence.

## Approval Controls

Approve and Reject buttons appear in the run thread only when an action is
ready for human review. The approval brief describes the artifact or execution
scope and what pressing Approve authorizes.

Current gates include:

1. protocol and evaluation-contract proposal
2. generated evaluation-contract promotion, when a new harness is required
3. experiment execution, after Honeydew methodology review and deterministic
   preflight
4. final report acceptance, after Honeydew verification and deterministic
   assessment of unresolved findings

Reject opens a reason form. Rejection feedback is stored and returned to the
appropriate agent for revision. An approval is not evidence that execution
succeeded; job and artifact records remain authoritative.

### Final acceptance and unresolved findings

Before the final-acceptance gate is raised, the orchestrator deterministically
assesses the latest Honeydew verification turn and records the result as a
`verification.assessed` event. The assessment combines findings the verifier
declared (structured contradictions, methodological limitations, advisory
disagreement) with mechanical checks on its citations: a claim that cites no
durable evidence, or cites an artifact, job, contract, or knowledge record
that does not resolve, becomes a `missing_evidence` finding.

When the assessment has unresolved entries, the acceptance brief lists them
before anything else variable-length, and approving requires acknowledging
them. The acknowledgement is recorded as an idempotent
`action.findings_acknowledged` event naming the reviewer and every unresolved
item, and the terminal `run.completed` event carries `findings_acknowledged`
plus `unresolved_findings_count`. HTTP approvers pass
`acknowledge_unresolved_findings: true` explicitly; Discord approvals
acknowledge through the disclosed brief itself. Clean assessments keep the
one-click path unchanged.

Findings are advisory metadata for humans, not verdicts: they never block the
workflow, and they are never generated by prose analysis of the report. Only
verifier-declared concerns and mechanically resolvable citations count.

## What COMPLETE guarantees

A run reaching `COMPLETE` guarantees that every workflow gate passed with its
recorded approvals, that successful jobs' immutable evaluator outputs were
integrity-clean, that the promoted report matches its registered digest, and
that any unresolved verification findings at acceptance time were disclosed
and explicitly acknowledged by the accepting human.

`COMPLETE` does **not** mean the scientific conclusions are flawless,
exhaustively replicated, or free of limitations. Exploratory runs may complete
while carrying acknowledged methodological limitations; later readers should
inspect `action.findings_acknowledged`, `verification.assessed`, and the
run's artifact digests rather than treating the terminal state as a peer
review verdict.

## Starting An Arbitrary Task

Create a ZIP with:

```text
any-directory-name/
  problem.md
  eval_agent_prompt.md  # optional
```

`problem.md` should state:

- research question and hypotheses
- dataset source
- split and leakage constraints
- required baselines and controls
- metrics and uncertainty requirements
- expected artifacts
- compute or stopping constraints

Then use:

```text
/task-start archive:<attach ZIP>
```

Honeydew compiles the text into a validated `glasslab-task-spec-v1`.
Deterministic policy, not the model, selects:

- `workspace-cpu-ml-v1` or `workspace-gpu-ml-v1`
- the allowlisted runner image
- command and Kubernetes workload shape
- CPU, memory, GPU, wall-clock, and parallelism ceilings
- the initial immutable evaluation contract

The task filename has no semantic meaning.

## Dataset Boundary

For a local dataset, upload it first:

```text
/dataset-upload dataset:<attach file> name:income role:train contains_labels:true
```

The bot returns `glasslab-dataset://<sha256>`. Put that exact reference in
`problem.md`. Honeydew preserves it in the TaskSpec, after which the
orchestrator resolves it to a read-only shared-storage object and verifies its
digest during task preflight.

The generic path also supports datasets and other assets that:

- have a public, globally routable HTTPS URL
- require no login, cookie, token, or private-network access
- are no larger than 2 GiB per asset
- pass redirect, address, size, and optional expected-checksum validation

The task ZIP itself is limited to 16 MiB. Dataset files embedded in the ZIP are
not an execution-data path; reference an uploaded dataset or approved asset
URL. Discord uploads are limited to 100 MiB by service policy and may also be
limited by the Discord server. The authenticated HTTP endpoint streams files
up to the configured 2 GiB ceiling.

Not yet supported through Discord:

- directory or multi-file upload as one logical dataset
- private MinIO/S3 object selection
- Kaggle or other authenticated downloads
- datasets requiring license acceptance
- arbitrary container images or system packages

Package multi-file data into one archive before upload, or use a reviewed
external ingestion process and register the resulting immutable object.

## Evaluation Boundary

The generic `generic-task-integrity-v1` contract verifies declared metric keys,
artifacts, checksums, and provenance. It does not prove a domain-specific
scientific conclusion.

When structural validation is insufficient:

1. Honeydew specifies the required evaluator behavior.
2. Beaker drafts a bounded evaluator candidate.
3. The orchestrator seals and checksums it.
4. Honeydew reviews the read-only sealed copy.
5. A human approves promotion into the trusted contract catalog.

Neither Hermes agent can edit a promoted contract or substitute an evaluator
entry point in a job request.

## HTTP Operator API

The HTTP API is for automation, recovery, and diagnostics. Operators should
not need to hand-write requests for normal Discord usage.

Read paths:

```text
GET /runs
GET /runs/{run_id}
GET /runs/{run_id}/events
GET /runs/{run_id}/events/stream
GET /runs/{run_id}/artifacts
GET /runs/{run_id}/turns
GET /task-bundles
GET /task-bundles/{task_id}
GET /task-bundles/{task_id}/preflight
GET /datasets
GET /datasets/{dataset_id}
GET /knowledge/sources
GET /actions/{action_id}
GET /health
GET /ready
```

State-changing paths require `X-Glasslab-Operator-Token` in the live
deployment:

```text
POST /runs
POST /runs/{run_id}/retry
POST /task-bundles/import
POST /datasets/import
POST /knowledge/sources
DELETE /knowledge/sources/{source_id}
DELETE /knowledge/sources/by-digest/{digest}
POST /knowledge/index/rebuild
POST /runs/{run_id}/pause
POST /runs/{run_id}/resume
POST /runs/{run_id}/cancel
POST /actions/{action_id}/approve
POST /actions/{action_id}/reject
```

Do not put the operator token, Discord token, or webhook URL in documentation,
Git, shell history, or screenshots.

## Inspecting A Run

Discord is the readable transcript, but the database, event log, job records,
and artifact registry are authoritative. The service is `ClusterIP` only. From
a contributor workstation, create a tunnel through the provisioner:

```bash
ssh -L 18080:127.0.0.1:18080 glasslab-provisioner \
  'sudo -n env KUBECONFIG=/home/glasslab/.kube/config \
   kubectl -n glasslab-v2 port-forward \
   svc/glasslab-research-orchestrator 18080:8080'
```

In another terminal:

```bash
RUN=<run-id>
curl -fsS "http://127.0.0.1:18080/runs/$RUN" | jq
curl -fsS "http://127.0.0.1:18080/runs/$RUN/events" | jq
curl -fsS "http://127.0.0.1:18080/runs/$RUN/artifacts" | jq
curl -fsS "http://127.0.0.1:18080/runs/$RUN/turns?limit=20" | jq
curl -N "http://127.0.0.1:18080/runs/$RUN/events/stream"
```

`GET /runs/{run_id}/turns` is a bounded, redacted convenience view over
already-persisted per-turn agent state (agent identity, structured input and
output, status, and start/end timestamps), with `limit` capped at 100 turns
per call. Secrets and credential-shaped content (Discord tokens, the operator
token, kubeconfig contents, model API keys, and similar) are scrubbed from
both input and output before the response is built. It is additive: the
normalized event log remains the authoritative record, and `/turns` never
supersedes it.

The run's workspaces, protocol, reports, OpenCode runtime data, and recovery
checkpoints are stored beneath
`/mnt/artifacts/research-orchestrator/runs/<run-id>/` in the orchestrator pod.
Use `kubectl exec` from the provisioner to inspect raw runtime storage beyond
what `/turns` exposes.

## Deployment Commands

GitHub Actions publishes a matched pair of immutable images under the full
commit SHA:

```text
ghcr.io/ccny-glasslab/glasslab-workflow-api:<full-sha>
ghcr.io/ccny-glasslab/glasslab-research-orchestrator:<full-sha>
```

Deploy that release from the canonical `.44` checkout:

```bash
cd /home/glasslab/cluster-config
./scripts/rollout-research-services.sh --sync
```

Roll back to a previously published release:

```bash
./scripts/rollout-research-services.sh --tag <full-commit-sha>
```

The rollout command does not build images locally. It applies service policy
and configuration, selects the exact images, waits for both Deployments, and
runs live readiness checks.

## Progress Snapshot

Implemented and live:

- separate Honeydew and Beaker OpenCode sessions and workspaces
- fresh-session recovery after failed or interrupted agent turns, with compact
  persisted checkpoints and unchanged worktrees
- a bounded Beaker planning turn before implementation, without a fixed
  experiment scaffold
- durable state, actions, jobs, artifacts, and append-only events
- protocol, evaluator-promotion, execution, and final-report approval gates
- Discord start, task-start, dataset upload, approval, rejection, pause,
  resume, cancellation, artifact-download, and redacted turn-history controls
- a bounded, redacted `GET /runs/{run_id}/turns` read endpoint over
  already-persisted per-turn agent state
- deterministic CPU/GPU task compilation, immutable uploaded datasets, and
  public asset ingestion
- bounded Kubernetes execution through `workflow-api`
- immutable evaluator enforcement and generated-contract promotion
- CI-published, commit-addressed control-service releases
- restart recovery and job reconciliation

Validated:

- 98 research-orchestrator tests
- 159 workflow-api tests
- mocked complete research workflow
- live Hermes/Qwen structured task compilation
- live Discord threads, identities, approvals, rejection feedback, and
  cancellation projection
- live Discord registration of dataset upload, pause, and resume commands
- live immutable dataset upload, durable lookup, and checksum readback
- live pause/resume recovery of the Adult run into a new Beaker turn
- live Kubernetes rollout and service readiness
- Adult, Wine, and Fashion-MNIST task preflight

Benchmark milestone:

- Adult Income run `cce710ceef97441685c777c8f19c767b` completed the
  orchestrator workflow and final acceptance path.
- Wine run `39101d9c9d3d4753bcd74e93e6106819` submitted cluster jobs. The
  workload calculations completed, but the immutable evaluator rejected the
  first results because `plots/clusters.png` was missing.
- Honeydew identified the missing evidence and Beaker added the plot. Beaker's
  final one-job replacement matrix passed deterministic preflight, but the
  overall research run reached `TIMED_OUT` before Honeydew review and execution
  approval.
- This validates failure evidence, revision-budget reset, artifact preflight,
  and one-job seed handling. It does not constitute a completed Wine result.
- Fashion-MNIST is preflight-ready but has not completed a live run.

The Adult, Wine, and Fashion-MNIST definitions are compatibility fixtures with
pre-registered datasets and task-specific evaluators. The generic extension
path is `/task-start`, not another hardcoded task entry.

## Current Limitations

- one active research run
- one orchestrator replica; PostgreSQL is durable, but horizontal scaling is
  deliberately deferred until active-run and Discord ownership policies are
  broadened
- fixed approved repository and runtime profiles
- no authenticated remote dataset download or private object-store browser
- no Discord list or status commands
- no first-class HTTP endpoint for complete structured turn inspection
- terminal retries are limited to verified `FAILED`/`TIMED_OUT` protocol
  checkpoints and always require fresh approvals; a terminal retry child is
  superseded by the next retry rather than reopened
- no automatic Git push or pull request creation
- no arbitrary SSH, `kubectl`, secret access, or container publication for
  either agent
- no completed live end-to-end arbitrary-dataset task yet
- the two-node 70B runtime is slow enough that agent turns use a 30-minute
  deadline; cluster jobs do not hold an agent turn open

For architecture and trust-boundary details, read
[`research-orchestrator.md`](research-orchestrator.md).
