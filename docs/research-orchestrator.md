# Glasslab Research Orchestrator

Status: deployed as a single-replica MVP. The complete workflow is covered with
mocked Hermes and cluster adapters. Hermes against the two-node exo model,
Discord threads and role-gated controls, restart recovery, and live Kubernetes
deployment were tested on 2026-07-29. Three imported ML benchmark task types
now have bounded CPU/GPU workload definitions and immutable evaluator contracts;
their live end-to-end execution status is recorded below. Generic TaskSpec
compilation and preflight were validated against the live agent runtime and Qwen with a
synthetic task; a complete arbitrary-dataset run remains outstanding.

For the concise operator surface, read
[`research-orchestrator-command-surface.md`](research-orchestrator-command-surface.md).

## Terminal-run retry checkpoints

Retries are new child runs, never reopenings or mutations of a `FAILED` or
`TIMED_OUT` parent. The initial implementation intentionally supports one
conservative checkpoint.

| Eligible checkpoint | Copied durable material | Excluded material | Child path | Renewed approvals | Validation and failure |
| --- | --- | --- | --- | --- | --- |
| `approved_protocol_v1` | task binding/digest plus fresh task preflight, approved `program.md`, resolved contract ID/version/digest, recorded base commit, bounded non-destructive worktree delta | jobs, runtimes, sessions, turns, actions, approvals, locks, parent Discord IDs, counters, artifacts other than protocol, terminal state | `PREPARING` then `AWAITING_PROTOCOL_APPROVAL` | protocol, any contract promotion, execution, final report | Every copied file is manifested and SHA-256 checked. The child is pinned to the parent base commit, has a fresh Discord thread, and reconstructs task inputs and `program.md` from authoritative copies. Missing, changed, ambiguous, oversized, symlinked, committed, renamed, or deleted worktree material fails the child closed. |

The retry relationship is committed transactionally and appears in both event
streams. While a child is still active, an equivalent repeated retry returns
that same child. Once a child has itself reached a terminal state
(`FAILED`, `TIMED_OUT`, `CANCELLED`), the next retry supersedes it: a fresh
child is created from the parent's verified checkpoint, the parent's retry
slot points at the new child, and the superseded child gains a durable
`run.retry_superseded` event without any other mutation. Discord only
projects the stored relationship and may fail without changing it.

Migration: startup creates the additive `terminal_run_retries` (SQLite) or
`orchestrator_terminal_run_retries` (PostgreSQL) table. Existing run payloads
remain valid because lineage fields are optional; no existing run is migrated
or made retryable without its verified checkpoint files.

## Purpose

The research orchestrator coordinates two isolated research agents around the
existing bounded execution plane:

```text
 human / Discord / HTTP
           |
           v
 +-----------------------+
 | research-orchestrator |
 | state, policy, events |
 +----+-------------+----+
      |             |
      v             v
 Honeydew        Beaker
 OpenCode        OpenCode
 runtime         runtime
      |             |
      +------+------+
             |
             v
      structured actions
             |
             v
        workflow-api
             |
             v
     approved runner Jobs
             |
             v
  artifacts + evaluation output
```

The agent runtime is the inner loop. It performs each agent's model call,
local tool loop, file changes, and structured response. The orchestrator is
the outer scientific workflow. It owns turn-taking, approvals, durable state,
privileged actions, evidence, interruption, and recovery.

This separation avoids another home-grown model tool loop and prevents model
prose from being confused with an authoritative action result.

## Division Of Labor

Honeydew owns research methodology and synthesis. It drafts `program.md`,
reviews Beaker's proposed implementation and matrix, checks evaluation output,
and writes `report.md`. Only Honeydew may draft the protocol. It has no cluster
credentials and cannot change Beaker's worktree.

Beaker owns implementation and experiment analysis. It edits its isolated
worktree, runs bounded local checks, and proposes normalized experiment
matrices. It cannot run `kubectl`, use SSH, push Git changes, read secrets, or
publish artifacts.

The evaluation contract is repository-controlled and immutable to both agents.
It fixes the evaluator entry point, schemas, required artifacts, resource
limits, optional digest-pinned image, and machine-checkable methodology
requirements. Methodology requirements distinguish comparisons, which need
multiple configured values, from decisions, which need one explicit choice.

When an approved protocol requires a harness that is not installed, Beaker may
draft a contract candidate in its isolated worktree. The orchestrator validates
and seals it, Honeydew reviews the sealed read-only copy, and a Discord
administrator must approve promotion. Neither agent can write the trusted
catalog.

The orchestrator alone validates structured outputs, classifies actions,
performs state transitions, expands approved matrices, and delegates jobs to
the bounded cluster execution service.

## State Machine

The implemented states are:

```text
CREATED -> PREPARING -> HONEYDEW_DRAFTING_PROTOCOL
  -> AWAITING_PROTOCOL_APPROVAL
  -> BEAKER_DRAFTING_CONTRACT -> HONEYDEW_REVIEWING_CONTRACT
  -> AWAITING_CONTRACT_PROMOTION
  -> BEAKER_PLANNING -> BEAKER_IMPLEMENTING
  -> BEAKER_FINALIZING (interrupted imported tasks with a runner checkpoint)
  -> HONEYDEW_REVIEWING
  -> BEAKER_REVISING (when requested)
  -> AWAITING_EXECUTION_APPROVAL
  -> JOB_QUEUED -> JOB_RUNNING
  -> BEAKER_ANALYZING -> HONEYDEW_VERIFYING
  -> HONEYDEW_WRITING_REPORT
  -> AWAITING_FINAL_ACCEPTANCE -> COMPLETE
```

`PAUSED`, `FAILED`, `CANCELLED`, and `TIMED_OUT` are explicit terminal or
control states. Transitions are validated in code. The agent may recommend a
next state but cannot perform the transition.

A failed Kubernetes job is stored as evidence and normally returns the run to
Beaker for analysis. It does not automatically fail the research run.

## Durable Records

The production service stores runs, turns, actions, jobs, artifacts, knowledge
records, and append-only events in PostgreSQL. Records are JSONB with typed
query columns; each event receives a monotonically increasing per-run sequence
inside the same transaction as its state change. A transaction-scoped advisory
lock makes the one-active-run policy and event ordering correct across process
boundaries. SQLite with WAL remains the local-development and smoke-test
backend, plus a one-time import source for the previous deployment.

Normalized event names form the stable external contract. Raw runtime event
names are translated into events such as `agent.tool_started`,
`agent.turn_completed`, `action.proposed`, `job.completed`, and
`artifact.recorded`.

## Workspaces And Agent Runtimes

Each run has this layout:

```text
runs/<run-id>/
  protocol/program.md
  beaker-worktree/
  honeydew-worktree/
  shared-artifacts/
  reports/
  events/
```

The worktree manager creates two detached Git worktrees from the one approved
repository. The approved protocol is copied read-only into each worktree.
Artifacts are copied through path-containment checks. Before methodology
review, the orchestrator copies the proposed config, implementation plan, and
bounded implementation files into Honeydew's `.glasslab-review/` directory.
The snapshot rejects symlinks and path escapes, enforces file and byte limits,
records SHA-256 digests, and is read-only. Honeydew therefore reviews Beaker's
actual candidate without gaining write access to Beaker's worktree.

The Hermes adapter starts one isolated Hermes process per agent. Each process
receives a separate workspace, configuration/data directory, system prompt,
permission configuration, server port, and session ID. Sessions are recorded
in the database and reconstructed after an orchestrator restart. Active turns
have an explicit abort path. Runtime event names are normalized before they are
persisted.

The current deployment configuration points both runtimes at:

```text
http://192.168.1.17:52415/v1
mlx-community/Qwen3-Coder-Next-4bit
```

That is the model identifier exposed by the checked repository configuration.
The service does not assume that the label `qwen3-coder-next-70b` is accepted by
the endpoint. Confirm the served model list before changing this value.

## Structured Turns

Every completed turn is validated as an `AgentTurnResult`. It contains a kind,
summary, evidence-backed claims, structured requested actions, an optional
message to the other agent, a recommendation, and a completion flag.

Evidence references must use `artifact://`, `git://`, or `event://`. A turn
cannot establish that a job ran. Only persisted job and artifact records can do
that.

### Structured-output failure handling

The orchestrator validates every turn and never infers intent from prose. A
turn that is malformed, schema-invalid, or of the wrong `kind` is rejected
rather than trusted, and each failure is distinguishable in the durable event
log:

- A turn that returns no JSON, unparseable JSON, or JSON that fails
  `AgentTurnResult` validation raises a runtime error whose `failure_class` is
  `not_text`, `malformed_json`, or `schema_invalid` respectively. The failed
  turn is recorded as `agent.turn_completed` with `status: failed` and that
  `failure_class` in the payload.
- A turn that validates but returns the wrong `kind` (for example
  `verification` where `protocol_draft` was required) is recorded as
  `agent.output_rejected` with both `returned_kind` and `expected_kind`. The
  orchestrator issues exactly one focused repair turn that names the only
  allowed kind, then fails the run if the repair is still wrong.

A repair turn is always placed after any retrieved/context material and may
only request actions that the policy layer already authorizes; it cannot
advance state or duplicate an action or job on its own. A protocol draft that
returns a valid `protocol_draft` but declares no produced `protocol` file — the
live contract violation observed in run `7a1cef60dd3b49e0b565759ea988edb6` — is
rejected as `agent.output_rejected` and repaired with one focused turn that
names `program.md`, requires purpose `protocol`, and forbids actions. The
repair is revalidated (exactly one declared protocol file that exists on disk)
before the run may advance to `AWAITING_PROTOCOL_APPROVAL`.

Known limitation: the focused repair is a single turn per failure class. A
runtime that repeats the same failure in its repair turn ends the run
`FAILED` rather than looping; resuming from a terminal state is not yet
supported and is tracked as terminal-checkpoint retry (#92).

## Evidence Snapshots

Agent turns that consume job output never receive raw cluster access or
unbounded files. Before `_analyze_results` (Beaker), `_verify_results`
(Honeydew), and `_write_report` (Honeydew), the orchestrator builds a compact,
phase-scoped snapshot with `build_evidence_snapshot(settings, store, run_id,
phase)` in `services/research-orchestrator/app/evidence.py`. The phase is one
of three `EvidencePhase` values:

- `ANALYSIS` gives Beaker job identity, status, exit, variant, and seed records
  (spec and requested_resources projected out) plus excerpts of `runner.log`,
  `status.json`, `evaluation.json`, `metrics.json`, `metrics.csv`, and
  `fairness.csv`.
- `VERIFICATION` gives Honeydew status-only job summaries plus `status.json`,
  `evaluation.json`, `metrics.json`, and `report.md`. It contains no
  `runner.log` and no CSV tables.
- `REPORT` gives Honeydew status-only job summaries plus `evaluation.json` and
  `metrics.json` only.

The engine wrapper `_evidence_snapshot(run_id, phase=EvidencePhase.ANALYSIS)`
keeps its previous default, so existing callers are unchanged. The artifact
inventory is phase-scoped like the contents: metadata for artifacts whose
content a phase never receives is not included, so an agent cannot cite URIs
for evidence it was never shown.

Artifact contents are deduplicated by `(sha256, type)`. The first occurrence in
store order keeps its content; later identical occurrences carry a
`duplicate_of` reference to the first URI. Two artifacts with the same digest
but different types are never collapsed, so a verbatim `metrics.json` cannot be
replaced by coincidentally identical `status.json` bytes. When the size budget
drops a content representative, its dependents are dropped with it so no
retained `duplicate_of` ever points at missing content.

`evaluation.json` and `metrics.json` are kept verbatim as fully parsed JSON up
to `evidence_verbatim_max_bytes` (64 KiB by default). An artifact beyond that
cap contributes a `content_omitted` reference instead of a head-truncated
partial JSON document, so evaluator failures and representative metrics are
never cut mid-JSON. All other excerpted files are bounded by
`evidence_excerpt_max_bytes` (32 KiB by default), with `runner.log`
tail-excerpted.

The whole snapshot is bounded by `evidence_snapshot_max_bytes` (512 KiB by
default). The budget measures the exact production serialization the engine
embeds in agent prompts (`serialize_evidence`: `json.dumps(snapshot, indent=2,
sort_keys=True, ensure_ascii=False)`) counted as encoded UTF-8 bytes, and it
includes the truncation note itself, so the prompt can never exceed the cap by
a serialization-shape mismatch. Trimming is least-protected-first: logs and
CSVs are dropped before the artifact inventory and job summaries, which are
dropped before `status.json`/`report.md`, and verbatim evaluator and metrics
content is retained longest (highest retention priority). Anything dropped
is recorded in a `truncation` note listing the omitted references — explicit
`artifact://...` and `job://...` references, one per trimmed artifact or job
summary (bounded to the
first 25 plus an `omitted_more_count`, so the note cannot grow without limit).
The cap is validated at Settings construction against
`EVIDENCE_SNAPSHOT_MIN_BYTES` (1024): the minimal satisfiable snapshot (empty
lists plus a count-only note) serializes to ~251 bytes, so every accepted
configuration can always be trimmed to honor the bound. Omitted URIs are
counted once per artifact, not once per snapshot-entry removal operation.
Complete artifacts always remain in the durable artifact store; the snapshot
is a lossy-but-referenced projection, never a deletion.

The three limits are additive settings in
`services/research-orchestrator/app/config.py`, exposed as
`GLASSLAB_ORCHESTRATOR_EVIDENCE_*_MAX_BYTES`. The builder treats the store as
read-only: no evidence is removed from the durable record, and there are no
schema or store-contract changes.

## Evaluation Integrity

Contracts live under
`services/research-orchestrator/evaluation-contracts/<id>/<version>`.
Resolution verifies:

- the declared ID and version
- input and output schema files
- the fixed evaluation entry point
- required artifacts and resource ceilings
- a SHA-256 digest over all contract content
- absence of symlinks

Generated contract candidates are bounded to safe text, Python, and JSON files,
syntax checked without executing candidate code, copied to a sealed directory,
and hashed by the orchestrator. The sealed bundle is then copied read-only into
Honeydew's workspace. Promotion installs that exact digest under:

```text
/mnt/artifacts/research-orchestrator/trusted-contracts/bundles/<id>/<version>
```

and atomically updates the shared trusted catalog. A protocol remains blocked
from experiment submission until its evaluator type, primary metric,
direction, required artifacts, and resource ceiling match the installed
contract.

Job proposals are recursively rejected when they attempt to supply evaluator
paths, contract mounts, contract files, entry-point overrides, or contract
digests. The deterministic Kubernetes renderer uses a digest-pinned init image,
copies the contract to an `emptyDir`, and mounts it read-only into the runner.

The authoritative `workflow-api` submission path independently resolves the
requested ID, version, and digest against its trusted contract catalog. For a
promoted shared bundle it independently re-hashes the content and descriptor,
then mounts the exact PVC subpath read-only. Image-backed contracts continue to
use a digest-pinned init image and read-only `emptyDir`. In either case it
replaces the runner command with the trusted wrapper. The wrapper executes the
registry-approved experiment entry point first and then the fixed evaluator.

The included example contract and image digest are test fixtures. Three
repository-controlled benchmark contracts are installed into the trusted
catalog at startup after checksum verification. Agent-generated contracts
still require Honeydew and human promotion approval. Unknown, changed, or
mismatched bundles fail closed.

## Knowledge Context Retrieval

The orchestrator maintains an append-only, content-addressed knowledge store
for durable context the agents may cite. It is separate from workspaces and
evaluation contracts: nothing an agent writes is ingested without an explicit,
path-allowlisted ingest operation.

Knowledge sources are ingested with an explicit `SourceType`:

- `documentation`, `handoff`, `paper`, `technique_card`, `evaluation_contract`,
  `run_protocol`, `run_report`, `run_artifact`, `implementation_file`,
  `task_bundle`, `dataset_metadata`

Every source records a SHA-256 digest, a canonical URI, a version, and an
evidence URI of the form `knowledge://<source_id>`. Re-ingesting identical
content from the same canonical URI deduplicates to the original source row so
its evidence URI stays stable; sources are invalidated explicitly by digest.

Retrieval is lexical and quality-ranked. The production backend uses PostgreSQL
full-text ranking; SQLite FTS5 remains the local fallback. Ranking is weighted
by exact query-term overlap. The final ranking preserves the anchor
behavior of lexical exact-match for distinct-topic queries so a
distinct-topic result cannot be displaced by a generic near-match. Embedding-
based semantic similarity and reranking are planned but not yet implemented.

Per-turn retrieval is scoped to the active agent's role and the turn kind.
Honeydew's protocol and review turns access methodology, evaluation,
run-record, and verified-result context; Beaker's planning and implementation
turns access protocol, repository, implementation, job-log, and bounded
artifact context. Implementation files are excluded from Honeydew protocol
drafts. The system boundary is enforced in retrieval, not only in prompt text.

Each agent turn receives a `ContextPacket` of ranked source chunks within the
configured token budget. Attachment is recorded as an
`agent.context_attached` event with the packet ID, agent, turn kind, ranked
count, and token count, and the packet is citable as
`knowledge://context:<packet_id>`. Report generation therefore can cite the
exact context packet that grounded a claim; a claim about knowledge requires a
`knowledge://` evidence URI.

## Compiled Research Tasks

The generic contribution path accepts a ZIP with exactly one `problem.md` and
zero or one `eval_agent_prompt.md`; its filename has no semantic meaning.
Honeydew reads the normalized files in a temporary isolated Hermes session
and returns a validated `glasslab-task-spec-v1` containing:

- a human-facing name
- one approved runtime profile
- uploaded-dataset references or public asset requirements and checksums
- required metric keys and evidence artifacts
- unresolved inputs that block execution

The model does not select a container image, command, workload ID, resource
ceiling, evaluator entry point, or Kubernetes fields. Deterministic code
compiles the proposal into either `workspace-cpu-ml-v1` or
`workspace-gpu-ml-v1`, downloads declared public HTTPS assets with size and
address checks, computes SHA-256, stores immutable references, and binds the
repository-controlled `generic-task-integrity-v1` contract.

Local datasets are ingested separately from task ZIPs. The HTTP and Discord
surfaces accept a bounded file, store it read-only under the shared artifact
mount, and register its SHA-256 digest in the durable store. The returned
`glasslab-dataset://<sha256>` reference is the model-facing identifier. Task
compilation resolves it to an `s3://artifacts/...` contract; preflight verifies
the file and digest, and `workflow-api` mounts the exact PVC subpath read-only
in the experiment job.

The generic contract verifies the task's declared metric keys and evidence
artifacts. If those structural checks are not scientifically sufficient,
Honeydew proposes a task-specific evaluator and the existing Beaker
candidate, Honeydew review, and human promotion workflow is used. The generic
gate is never treated as proof of a domain-specific scientific claim.

Import rejects links, traversal, unsafe paths, and oversized archives. Task
preflight blocks run creation when inputs are missing, an image is not
allowlisted, or the evaluator is unavailable. Beaker writes executable code
under `research-workspace/<task-id>/`; approved submission creates a
deterministic `source.zip` and sends only digest-pinned task, source, and asset
references through `workflow-api`.

The three previously imported Adult, Wine, and Fashion-MNIST records remain
readable and keep their original task-specific contracts and workload IDs.
The old registry entries and `/benchmark-start` command are compatibility
aliases, not the extension mechanism for new tasks.

Start from Discord:

```text
/task-start archive:<attach task ZIP>
```

Or use HTTP:

```bash
curl -fsS -X POST http://127.0.0.1:8080/task-bundles/import \
  -H "X-Glasslab-Operator-Token: $TOKEN" \
  -F "archive=@$HOME/Downloads/my-research-task.zip"

curl -fsS \
  "http://127.0.0.1:8080/task-bundles/<task-id>/preflight?digest=<sha256>"

curl -fsS -X POST http://127.0.0.1:8080/runs \
  -H "X-Glasslab-Operator-Token: $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"objective":"Complete and evaluate this imported task.","task_id":"<task-id>","task_bundle_digest":"<sha256>"}'
```

The legacy benchmark dataset staging helper remains available for the three
existing compatibility records:

```bash
./scripts/stage-ml-benchmark-datasets.sh
```

New tasks declare assets in their TaskSpec instead of requiring code changes or
a catalog entry.

## Actions And Jobs

Policy is deterministic:

| Action | Decision |
|---|---|
| Isolated reads, edits, and local tests | automatic |
| Experiment-branch commit | automatic and audited |
| Protocol update | Honeydew only |
| Evaluation-contract modification | denied |
| Draft evaluation-contract candidate | Beaker only |
| Promote sealed evaluation contract | Honeydew review and human approval |
| Small validation job | Honeydew approval |
| GPU job | Honeydew and human approval |
| Git push, PR, or publication | human approval |
| Secret read or shared-resource deletion | denied |

Images, CPU, memory, GPU, matrix size, parallelism, namespace, and contract
integrity are checked before submission. Matrix expansion is canonical and
deterministic across variants and seeds. Every expanded job receives a stable
idempotency key.

Before an experiment matrix can reach human approval, deterministic preflight
also verifies that its base configuration exists in Beaker's worktree, the
evaluation-contract digest is unchanged, every requested resource fits the
contract's own ceilings, and the config satisfies the contract's declared
comparisons and decisions. It syntax-checks Python workspace code and rejects
workload references to evaluator-owned `evaluation.json`, `rubric_score`, and
`integrity_pass`. Workloads emit metrics and evidence; the immutable wrapper
runs the evaluator and owns evaluation output. Honeydew's structured approval
cannot bypass these checks.

The same ownership rule applies during protocol generation. A protocol may
list evaluator output as a final artifact, but it cannot assign creation,
formatting, reading, or scoring of that output to Beaker or workload code.

The preflight report records the exact expanded job count, checks performed,
configured comparisons, configured decisions, and blocking findings. Discord
renders that report before showing approval controls.

The original Adult benchmark contract remains immutable at `1.0.0`.
Methodology declarations were added as `ml-benchmark-adult-income-v1@1.1.0`;
new Adult task runs use the newer binding while historical runs retain their
recorded `1.0.0` digest.

Honeydew rejection feedback is passed to Beaker with the complete structured
claim list and evidence references. Automatic methodology repair is limited by
`GLASSLAB_ORCHESTRATOR_MAXIMUM_METHODOLOGY_REVISIONS`, two by default.
Exceeding the limit pauses at `BEAKER_REVISING` and emits
`methodology.human_resolution_requested` instead of consuming the remaining
turn budget in an unbounded review loop.

Approval and execution are separate audited facts. If an approved action cannot
execute, the orchestrator records `action.execution_failed` with the error,
authoritative job and artifact counts, retry classification, resulting safe
state, and next step. A deterministic matrix failure is marked
`execution_failed` and returned to Beaker for revision. A transient runtime or
infrastructure failure preserves the approval and pauses the run for
reconciliation and explicit resume.

The `workflow-api` adapter uses the existing approved workload API and never
passes Kubernetes credentials to an agent. `workflow-api` now owns the trusted
evaluation-contract catalog and read-only wrapper mount. It does not yet
provide a remote idempotency-header contract or a confirmed cancellation
endpoint. The orchestrator preserves local idempotency, but a crash between a
successful remote submission and local persistence remains a live-integration
gap.

## Long Jobs And Recovery

Agent turns end before submission. While jobs run, Hermes is idle and the
watcher reconciles authoritative job state. Completion records exit details and
artifacts before beginning a new agent turn.

At startup the service:

1. marks interrupted active turns for audit,
2. reloads nonterminal runs,
3. rotates any interrupted agent session and writes a compact recovery
   checkpoint while preserving the worktree,
4. reconciles `JOB_QUEUED` and `JOB_RUNNING` jobs, and
5. advances workflows only after authoritative evidence is stored.

Cancellation aborts active Hermes turns and requests cancellation for every
nonterminal job. Prior events are retained.

Pause records the exact state to resume. If recovery after resume fails, the
orchestrator records the failed turn, terminates that agent's Hermes process,
clears the stale session ID, writes
`events/<agent>-recovery-checkpoint.json`, and returns the run to `PAUSED`.
Resume creates a fresh Hermes session, injects the compact checkpoint, and
continues from the unchanged worktree. Successful sessions remain reusable
across normal turns. Resume also detects older paused records whose latest
failed turn still references the attached session and rotates them before
recovery. A pause or cancellation received while an agent turn is completing
is rechecked after the turn output is stored; the output remains auditable, but
the orchestrator does not record requested actions or start another turn.

The run-level runtime ceiling measures active workflow time. The orchestrator
accumulates elapsed active seconds when a run is paused, stops the clock while
it remains `PAUSED`, and starts it again on resume. Operator review time in
explicit approval states remains part of active runtime unless the run is
paused.

Beaker implementation is split into two bounded turns. `BEAKER_PLANNING`
produces a task-specific `implementation-plan.md`; `BEAKER_IMPLEMENTING`
executes that plan and may adapt it when repository evidence requires. The
orchestrator does not impose a generated runner scaffold or a fixed model
architecture.

If an imported-task implementation turn is interrupted after creating its
required `run.py`, recovery enters `BEAKER_FINALIZING`. That bounded turn
preserves the existing implementation, runs only narrow local checks, repairs
concrete blockers, and proposes the experiment matrix. It does not restart the
broad implementation task or execute the full benchmark locally.

## Discord

Discord is an optional projection. One run maps to one thread, with semantic
Honeydew, Beaker, and Orchestrator messages and one editable status message.
Messages are rendered from persisted events after transaction commit. No
token-by-token output is posted, and Discord history is never used as memory.

The guild-scoped `/research-start objective:...` command is the human front
door. It invokes the same authoritative `create_run` engine method as the HTTP
API, then creates the run thread. The command is role-gated using the same
configured Discord control policy as approvals. The HTTP endpoint remains an
internal automation and recovery interface; operators are not expected to
construct it by hand for normal work.

`/dataset-upload` registers a bounded attachment in the immutable dataset
registry and returns a `glasslab-dataset://<sha256>` reference.
`/research-pause`, `/research-resume`, and `/research-cancel` resolve the run
from its thread, or accept an explicit run ID in the main channel. They record
the Discord actor and optional reason in the append-only event history.

The bot creates public threads and owns the editable status message. An
optional channel webhook posts semantic events with per-message Honeydew,
Beaker, and Orchestrator identities. Agent turn messages include the explicit
`message_to_other_agent` handoff stored in the authoritative event. The
webhook cannot approve actions or alter workflow state.

Approval messages are decision briefs rather than bare action IDs. They state
the research objective, the artifact or experiment scope under review, per-job
resources and concurrency where applicable, the evaluation contract, the gate
reason, and exactly what approval authorizes. A matrix proposal is first posted
without controls while Honeydew reviews it. Approve and Reject controls appear
only after deterministic preflight and Honeydew methodology approval succeed.
Post-approval execution failures are posted publicly in the run thread from the
persisted failure event; an ephemeral interaction response is not the only
failure signal.

Honeydew's protocol turn also returns a schema-validated logical evaluation
contract proposal: evaluator type, primary metric, direction, minimum effect,
guardrails, required artifacts, budget mode, resource ceilings, and rationale.
The orchestrator stores it as a checksummed artifact and shows it in the
protocol approval brief. The proposal cannot contain executable paths, images,
commands, or digests. Those remain in the repository-controlled immutable
harness. The brief explicitly reports whether the proposal is compatible with
the currently bound harness or requires a new trusted harness.

The bot requires only View Channel, Send Messages, Read Message History,
Create Public Threads, and Send Messages in Threads on the configured channel.
It does not require Administrator. When controls are enabled, the bot maintains
an outbound Gateway connection and posts Approve and Reject buttons on pending
actions. No public callback ingress is required. Each interaction is checked
against the configured guild, run thread, pending action, and immutable admin
role or user IDs before invoking the same authoritative engine methods as the
HTTP API. The Discord user ID and display name are stored as the reviewer.
Buttons acknowledge immediately; long agent work continues asynchronously.

The bot token and webhook URL belong in the ignored local Kubernetes Secret.
Application, guild, channel, and approval-role IDs are non-secret deployment
configuration. Glasslab currently authorizes the `Mystic Arts Masters` role
by ID. Discord role membership is therefore the operational approval policy.

## HTTP API

The service provides:

```text
POST /runs
POST /task-bundles/import
POST /datasets/import
GET  /datasets
GET  /datasets/{dataset_id}
GET  /task-bundles
GET  /task-bundles/{task_id}
GET  /task-bundles/{task_id}/preflight
GET  /runs
GET  /runs/{run_id}
GET  /runs/{run_id}/events
GET  /runs/{run_id}/events/stream
GET  /runs/{run_id}/artifacts
POST /knowledge/sources
GET  /knowledge/sources
DELETE /knowledge/sources/{source_id}
DELETE /knowledge/sources/by-digest/{digest}
POST /knowledge/index/rebuild
POST /runs/{run_id}/pause
POST /runs/{run_id}/resume
POST /runs/{run_id}/cancel
GET  /actions/{action_id}
POST /actions/{action_id}/approve
POST /actions/{action_id}/reject
GET  /health
GET  /ready
```

Deployment requires `X-Glasslab-Operator-Token` on all state-changing
endpoints. Health, readiness, run reads, events, artifacts, and SSE remain
read-only. Local development leaves this check disabled unless
`GLASSLAB_ORCHESTRATOR_REQUIRE_OPERATOR_AUTH=true`.

Normal Discord usage is:

```text
/research-start objective: Compare naive and semi-hard triplet mining on unseen CIFAR-100 classes.
```

Imported task usage is `/task-start` with a ZIP attachment. The command
compiles, preflights, and starts only ready tasks. `/benchmark-start` remains
as a compatibility alias. All start commands are restricted to the configured
channel and Discord administrator role.

Upload a local dataset before starting a task:

```text
/dataset-upload dataset:<attach file> name:training_data role:train contains_labels:true
```

Put the returned `glasslab-dataset://<sha256>` reference in `problem.md`.

Pause and resume from the run thread:

```text
/research-pause reason: Hold while checking the dataset.
/research-resume reason: Dataset check complete.
```

Cancel an active run from its Discord thread:

```text
/research-cancel reason: Superseded by a newer experiment.
```

From the configured main channel, provide `run_id`. Cancellation aborts active
Hermes turns, requests cancellation of active cluster jobs, records the
administrator identity and reason, and publishes the durable cancellation
event back to the run thread.

## Local Development

```bash
cd services/research-orchestrator
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
PYTHONPATH=. pytest -p no:cacheprovider -q
PYTHONPATH=. python3 -m app.smoke
```

The repository smoke wrapper is:

```bash
./scripts/smoke-test-research-orchestrator.sh
```

It needs no GPU, Qwen endpoint, Kubernetes access, or Discord token. It
demonstrates knowledge ingestion and retrieval, objective, protocol approval,
implementation, review, fake job approval and completion, analysis,
verification, context-cited report, final acceptance, and `COMPLETE`.

Final acceptance carries an explicit integrity semantic: the acceptance gate
records a deterministic assessment of the verification turn (agent-declared
findings plus mechanically resolvable citations), discloses unresolved
entries in the approval brief, and requires acknowledgement when any remain.
`COMPLETE` therefore guarantees process integrity with durable,
inspectable records of anything the human knowingly accepted past — not
scientific flawlessness. See the operator surface documentation for the
exact event names and acknowledgement flow.

Configuration is documented in
`services/research-orchestrator/.env.example`. Never commit the Discord token or
other live credentials.

## Deployment

The image is built from `services/research-orchestrator/Dockerfile`. Manifests
are under `kubeadm/glasslab-v2/research-orchestrator` and are included by
`scripts/deploy-glasslab-v2.sh`.

The manifest enforces one replica, disables service-account token mounting,
runs as a non-root user, and stores workspaces on
`glasslab-shared-artifacts`. An init container maintains the approved
repository checkout.

Before deployment:

1. publish the orchestrator image and pin the desired tag or digest,
2. verify the standard CPU/GPU workspace runner images are published,
3. verify the Qwen endpoint and exact model ID from the target node,
4. configure the published contract in the workflow-api trusted catalog,
5. validate workflow submission, status, artifacts, idempotency, and cancel,
6. create a local Discord secret only when Discord is enabled, and
7. deploy from the canonical checkout on `.44`, and
8. run `scripts/stage-ml-benchmark-datasets.sh` only for legacy benchmark data.

## Legacy Relationship

The Titanic agent stack remains under `services/agent-api`, `services/runner`,
and `kubeadm/agent-stack`. It is preserved as v1 reference material.

The orchestrator does not copy its Titanic-specific intent parser, legacy SQLite
schema, or direct Kubernetes submission model. It reuses the lessons and the
bounded execution boundary represented by `workflow-api` and
`research-workspace-runner`. The old stack can continue to run during migration.

## Validation Status

Implemented:

- state machine, durable records, ordered events, approvals, and recovery
- isolated worktree and Hermes runtime adapters
- structured turn validation and normalized runtime events
- contract digest checks and read-only job rendering
- policy, quotas, matrix expansion, fake and workflow-api cluster adapters
- HTTP API, SSE, Discord renderer, manifests, and configuration
- model-produced TaskSpec validation, deterministic CPU/GPU profile compilation,
  immutable asset ingestion, task preflight, and generic integrity evaluation
- knowledge ingestion, lexical-similarity retrieval, role-scoped per-turn
  context packets, and `knowledge://` citation evidence URIs
- retrieval quality fixtures and knowledge API surface

Covered by mocks:

- the full Honeydew/Beaker workflow
- structured Hermes turn completion and abort behavior
- parallel fake jobs, completion, failure evidence, and artifacts
- restart reconciliation and cancellation
- knowledge ingestion, retrieval scoping, and context attachment

Manually tested:

- Hermes runtime health and structured turn completion against Qwen
- the non-root service image build, Hermes version, application import, and
  `/ready` response
- a real structured Honeydew turn through Hermes and the exo-served
  `mlx-community/Qwen3-Coder-Next-4bit` model from `.44`
- live Discord public-thread creation, editable status publication, and
  Honeydew/Beaker webhook identities in the configured guild and channel
- live guild registration of `/dataset-upload`, `/research-pause`, and
  `/research-resume`
- live immutable dataset upload, durable lookup, and SHA-256 readback
- live resume of a paused Adult run into a new Beaker implementation turn
- distributed Qwen inference across the cabled `.17` and `.18` exo pair

Not yet tested:

- full Adult, Wine, or Fashion-MNIST benchmark completion on live GPUs/CPUs
- research-orchestrator submission of an experiment Kubernetes Job; the active
  Adult run has returned a structured matrix but has not passed methodology
  review and human execution approval
- full generic `/task-start` execution through final report acceptance
- automatic public asset ingestion against a real new task

## MVP Limitations

- one orchestrator replica and one active research run
- PostgreSQL live migration and recovery against the cluster database
- one approved repository and fixed agent profiles
- one process per agent, not a separate pod or Unix identity
- no Git push, PR creation, arbitrary SSH, or raw Kubernetes access
- no autonomous literature subsystem
- deterministic task gates validate required evidence and core invariants;
  Honeydew still performs the detailed rubric assessment

## Files

The implementation is contained in:

- `services/research-orchestrator/`
- the evaluation-contract enforcement path in `services/workflow-api/`
- `kubeadm/glasslab-v2/research-orchestrator/`
- `scripts/smoke-test-research-orchestrator.sh`
- `scripts/stage-ml-benchmark-datasets.sh`
- `docs/research-orchestrator.md`

CI, pre-push checks, deployment orchestration, and current documentation indexes
are updated to include the service.
