# ADR 0003: Hermes and Glasslab Control-Plane Boundary

Status: proposed

Issue: [#154](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/154)

## Decision

Hermes owns agent behavior. Glasslab owns scientific execution and durable
authority.

Hermes may coordinate Honeydew and Beaker sessions, choose the next agent turn,
maintain conversational context, and invoke skills. It must access Glasslab
through narrow, authenticated domain tools. It must not receive database
credentials, Kubernetes credentials, artifact-store credentials, or a general
remote shell.

Glasslab remains the authority for anything that must remain true after a model
restart or a provider outage:

- run and job records, legal state transitions, concurrency, and cancellation;
- approvals, policy classification, quotas, and idempotency;
- task and dataset validation;
- immutable evaluation contracts and their digests;
- Kubernetes submission and watcher reconciliation;
- artifact registration, checksums, provenance, and report acceptance;
- normalized events and recovery audit history.

The current `ResearchOrchestrator` service remains the compatibility boundary
during migration. It is not the long-term owner of agent prompting or session
orchestration merely because it currently contains those concerns.

## Responsibility classification

| Current responsibility | Destination | Authority rule |
| --- | --- | --- |
| Honeydew/Beaker prompt profiles | Hermes profiles/skills | Hermes may propose; Glasslab validates resulting actions |
| Agent turn sequencing and delegation | Hermes | Glasslab receives durable turn/action events and can pause/cancel |
| OpenCode/Hermes process lifecycle | Hermes runtime adapter | Glasslab stores references and health, not provider internals |
| Structured-output parsing | Hermes adapter or MCP client boundary | Glasslab validates the action schema before execution |
| Run state and persistence | Glasslab domain API | Hermes cannot write state directly |
| Approvals and policy | Glasslab domain API | Human and deterministic policy decisions are authoritative |
| Evaluation contracts | Glasslab contract service | Read-only contract identity/digest is supplied to Hermes |
| Dataset/task preflight | Glasslab domain API | No agent assertion substitutes for preflight |
| Kubernetes jobs | Glasslab bounded executor | Hermes submits a normalized proposal, never a manifest or `kubectl` call |
| Artifacts and provenance | Glasslab artifact registry | Results are accepted only from authoritative jobs and verified files |
| Discord projection | Glasslab adapter | Discord is a projection, never agent memory or state |
| Retry/recovery policy | Glasslab initially | Hermes may explain or propose recovery; Glasslab decides legal transitions |

## Target architecture

```text
                         human / Discord / local operator
                                      |
                                      v
       +---------------- Hermes ----------------+
       | Honeydew skill <-> Beaker skill        |
       | sessions, delegation, context, turns   |
       | model/tool loop, bounded local tools   |
       +------------------+---------------------+
                          | authenticated MCP/domain calls
                          v
       +----------- Glasslab control API -----------+
       | runs, approvals, policies, tasks, contracts |
       | jobs, artifacts, events, recovery           |
       +------------------+--------------------------+
                          |
                          v
       +---------- bounded execution layer ----------+
       | Kubernetes jobs, immutable evaluator, NFS/S3 |
       +----------------------------------------------+
```

The API is a domain boundary, not a generic MCP passthrough. Each operation
must verify its own preconditions. For example, `propose_job` validates the
normalized request against the task bundle, contract, image allowlist,
resource ceiling, and run state. `accept_result` independently verifies job
completion, artifact digests, evaluator output, and required approvals.

## MCP surface

The first MCP surface should be small and read-heavy:

| Tool | Effect | Required checks |
| --- | --- | --- |
| `get_run_context` | read | caller/run binding and redaction |
| `get_task_bundle` | read | immutable task ID and digest |
| `get_contract` | read | approved contract ID/version/digest |
| `record_protocol` | write proposal | Honeydew role, schema, run state |
| `record_implementation` | write proposal | Beaker role, workspace binding |
| `propose_job` | create pending action | task/contract/image/resources/matrix preflight |
| `inspect_job` | read | job belongs to run and current caller |
| `list_artifacts` | read | artifact ownership and redaction |
| `propose_revision` | write proposal | revision budget and evidence references |
| `request_approval` | create approval request | policy and idempotency |

The following are deliberately absent: raw `kubectl`, arbitrary SSH, secret
reads, unrestricted file access, direct state transitions, direct report
acceptance, and arbitrary outbound network fetches.

## What can be removed from ResearchOrchestrator

Removal should follow evidence, not a large rewrite. These are the likely
deletion or extraction candidates after Hermes-backed equivalence tests exist:

1. Prompt construction for Honeydew and Beaker.
2. Agent-turn sequencing that exists only to make two provider sessions take
   turns.
3. Provider-specific session rotation and process restart logic.
4. Provider-specific structured-output repair prompts.
5. Agent conversation history that duplicates Hermes session state.

The following must not be deleted merely to reduce code size:

1. Transactional state transitions and event ordering.
2. Action persistence, approval records, and idempotency keys.
3. Contract/task/worktree integrity checks.
4. Job submission, watcher reconciliation, artifact verification, and
   terminal retry checkpoint validation.
5. Discord rendering from persisted events.

## Migration plan

### Phase 1: document and observe

- Keep `ResearchOrchestrator` as the outer service.
- Run Hermes through the existing `AgentRuntime` adapter.
- Emit normalized runtime events and record provider/session IDs.
- Compare Hermes and OpenCode smoke outputs without allowing either to bypass
  Glasslab actions.

### Phase 2: expose domain tools

- Add a versioned domain-tool interface beside the HTTP API.
- Implement read-only context tools first.
- Add proposal tools whose only writes are durable pending actions.
- Require the same policy and schema validators used by HTTP and Discord.

### Phase 3: move agent coordination

- Move Honeydew/Beaker delegation and turn scheduling into Hermes skills.
- Make the Glasslab service event-driven: consume validated proposals and emit
  evidence/approval requests.
- Keep Glasslab able to pause, cancel, and recover a run if Hermes disappears.

### Phase 4: remove provider coupling

- Delete OpenCode-specific prompt and session logic only after Hermes and the
  mock runtime pass the same workflow contract tests.
- Retain a runtime adapter interface so a provider rollback remains possible.
- Keep the durable run schema and event vocabulary provider-neutral.

### Phase 5: simplify the service boundary

- Split the remaining `ResearchOrchestrator` responsibilities into a small
  domain/control module and adapters for HTTP, Discord, MCP, storage, and jobs.
- Remove only compatibility endpoints whose callers and deployment manifests
  have been migrated.

## Recovery and source of truth

Hermes session state is recoverable convenience state, not authoritative
workflow state. After a restart, Glasslab reconstructs the next legal action
from its database, events, approvals, job records, and checkpoint manifests.
It may start a fresh Hermes session with a compact evidence snapshot.

No operation is considered complete because Hermes said it happened. Completion
requires an authoritative record: an approved action, a Kubernetes observation,
or a verified artifact. Discord and Hermes may both be unavailable without
invalidating the durable run.

## Acceptance criteria for migration

Do not remove the current orchestrator responsibilities until all of the
following are demonstrated:

- a Hermes-backed run survives process restart without duplicated jobs or
  approvals;
- every privileged operation is represented by a validated domain action;
- task, contract, worktree, artifact, and approval invariants have equivalent
  tests through the Hermes path;
- pause, resume, cancel, and terminal retry work while Hermes is unavailable;
- Discord failure does not lose state or prevent later recovery;
- the complete smoke path works with a fake Hermes runtime and no cluster
  credentials;
- one live arbitrary-dataset run completes with independently inspectable
  events and artifacts.

## Risks

- Hermes and Glasslab could develop competing run histories. Glasslab events
  and records must remain the only workflow history.
- Moving retry logic too early could make terminal checkpoint integrity depend on
  an agent prompt. Keep retry validation deterministic until proven otherwise.
- MCP expands the tool boundary. Treat every tool as a privileged API endpoint:
  authenticate, authorize, validate, audit, and make writes idempotent.
- Provider changes can alter structured-output behavior. Contract tests must
  validate semantics and evidence, not only successful text generation.
