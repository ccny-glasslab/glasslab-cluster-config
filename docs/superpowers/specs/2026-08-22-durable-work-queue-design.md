# Durable Work Queue Design

## Status

Proposed architecture roadmap for later review. This document records the
direction and the decision process; it does not authorize a rollout.

## Goal

Replace bespoke background execution and recovery code with a mature durable
task queue, while preserving Glasslab's explicit approval, policy, provenance,
and deterministic execution boundaries.

The initial useful outcome is unattended, review-only repository work that can
run on the provisioner and leave reports or candidate commits in disposable
worktrees. The broader outcome is a simpler execution model for Honeydew,
Beaker, `research-orchestrator`, and `workflow-api`.

## Current Problem

Glasslab currently has several partial queue mechanisms:

- `research-orchestrator` executes Honeydew and Beaker turns inline and owns
  custom state recovery.
- experiment jobs have a per-run queued state and custom capacity-filling loop.
- `workflow-api` stores schedules and execution records.
- `schedule-worker` is a stateless HTTP driver whose cadence and retry behavior
  must be supplied externally.
- NATS has placeholder manifests but is not an authoritative event or work
  queue.

These mechanisms encode important domain policy, but they also make the API
services responsible for task claiming, retries, concurrency, and worker
lifecycle. Those generic responsibilities should move to maintained queue
infrastructure.

## Architectural Boundary

Queue infrastructure owns:

- durable task publication and claiming
- worker concurrency and queue routing
- delayed delivery and periodic dispatch
- retry timing and exponential backoff
- task expiry and dead-letter handling
- worker health and lifecycle
- standard queue observability

Glasslab code continues to own:

- research run and approval state
- authorization and approval-tier decisions
- idempotency keys for externally visible effects
- artifact and event provenance
- resource and concurrency policy
- validation before deterministic cluster execution
- the decision whether a failed operation is safe to retry

A queued task is a request to attempt one typed operation. It is never proof
that the operation remains authorized. Every handler must re-read authoritative
state and fail closed before producing an external effect.

## Service Boundary

Keep `research-orchestrator` and `workflow-api` as separate services:

- `research-orchestrator` owns research reasoning, agent collaboration, and
  human-facing research state.
- `workflow-api` owns deterministic workflow validation and bounded Kubernetes
  execution.

Both may publish and consume typed tasks through the same queue infrastructure,
but neither may mutate the other's authoritative tables directly. Existing
HTTP contracts remain the service boundary for cross-service commands.

## Queue Technology Decision

The first implementation phase is an evidence-producing comparison, not an
assumption that the repository should build its own queue.

Evaluate these two production shapes:

### Celery With RabbitMQ

Advantages:

- mature, widely deployed Python task framework
- established worker, routing, retry, scheduling, and monitoring ecosystem
- RabbitMQ provides a purpose-built durable broker
- minimizes queue-algorithm code owned by Glasslab

Costs:

- adds RabbitMQ and its backup, monitoring, upgrade, and credential lifecycle
- task results still need an explicit durable backend and retention policy
- transactional publication alongside Postgres domain-state changes requires
  an outbox or equivalent reconciliation design

### Mature Postgres-Native Python Queue

The first candidate is Procrastinate because it uses Postgres directly and
supports Psycopg, retries, locks, periodic tasks, and ASGI applications.

Advantages:

- uses the Postgres service and Python stack already deployed
- permits atomic domain mutation and task publication in one database
  transaction when the library supports the required connection boundary
- adds fewer services and fewer independent failure modes

Costs:

- smaller maintainer and operator ecosystem than Celery/RabbitMQ
- queue load shares resources with authoritative application state
- the project currently advertises a need for additional maintainers, which
  must be treated as a lifecycle risk

### Decision Criteria

Choose the implementation only after a disposable proof measures and verifies:

1. crash recovery after terminating a worker during a task
2. redelivery behavior and duplicate-effect protection
3. delayed retry and bounded backoff
4. queue-specific concurrency limits and routing
5. scheduled task behavior across worker and broker restarts
6. cancellation or revocation semantics for not-yet-started work
7. result and dead-letter inspection from a documented operator command
8. metrics, health checks, and stalled-work alerts
9. backup and restore requirements
10. dependency maintenance, release cadence, and Python 3.11 compatibility
11. clean integration with FastAPI and Psycopg 3
12. total operational burden on the current Glasslab cluster

Prefer Celery/RabbitMQ if both candidates satisfy correctness and its operational
cost is acceptable. Its maturity better matches the principle of delegating
generic infrastructure to experienced maintainers. Prefer the Postgres-native
candidate only if atomic publication and substantially lower operations burden
produce a clear advantage without sacrificing recovery or observability.

Do not use Celery's SQLAlchemy database transport as a compromise. Evaluate
Celery with a supported production broker or evaluate a queue designed around
Postgres.

## Typed Work Model

Initial task families are deliberately narrow:

- `repository.review`: inspect one immutable commit and produce a report
- `repository.candidate_patch`: work in a disposable worktree and produce a
  local candidate commit without pushing or merging
- `agent.turn`: run one Honeydew or Beaker turn from durable inputs
- `research.resume`: reconcile and advance one recoverable research run
- `schedule.execute_due`: invoke one bounded schedule execution cycle

Task payloads contain identifiers and immutable references, not secrets or
large mutable domain objects. Handlers retrieve authoritative records from the
owning service or store.

Candidate patches are permitted only in disposable worktrees. Unattended tasks
may not push branches, open or merge pull requests, deploy, modify cluster
infrastructure, decrypt SOPS material, or obtain additional credentials.

## Data And Control Flow

1. An API route, Discord command, scheduler, or reconciliation loop validates a
   request and records the relevant domain command.
2. The producer publishes a typed task with an idempotency key.
3. A queue worker claims the task under a named queue and concurrency policy.
4. The handler reloads authoritative state and repeats policy checks.
5. The handler invokes a focused domain operation.
6. Domain events and artifacts record the outcome independently of transient
   queue logs.
7. The handler acknowledges success only after durable outcome recording.
8. A retry reuses the same effect-level idempotency key.

Queue delivery is at-least-once. Glasslab must not claim exactly-once execution.
Externally visible effects are protected through domain idempotency and
reconciliation.

## Failure Model

- A worker crash before acknowledgement causes redelivery.
- A broker outage prevents new claims but does not authorize fallback shell
  execution.
- A Postgres outage prevents authoritative validation, so handlers fail closed.
- Exhausted retries produce an inspectable dead-letter or terminal task record
  and a Glasslab event.
- Stale worktree tasks are quarantined for inspection and later cleanup.
- Restarting an API service does not lose accepted work.
- Restarting a worker does not require manually reconstructing its inputs from
  logs.

## Migration Roadmap

### Phase 0: Product Evaluation

Build disposable Celery/RabbitMQ and Postgres-native probes implementing the
same no-op and crash-recovery tasks. Record the decision criteria above and an
operator assessment. Do not connect either probe to live execution authority.

Deliverable: an architecture decision record selecting one stack, including
rejected alternatives and measured recovery behavior.

### Phase 1: Review-Only Provisioner Worker

Deploy a worker controller on `.44` with a dedicated Unix identity, bounded
CPU/memory, explicit queue routing, and disposable worktree management. Support
`repository.review` first, followed by `repository.candidate_patch`.

Deliverable: unattended analysis that survives restarts and leaves inspectable
reports or local candidate commits, with no push, merge, secret, or deployment
authority.

### Phase 2: Durable Agent Turns

Extract one agent-turn boundary from `research-orchestrator`. Start with a
single Honeydew or Beaker state whose inputs and output contract are already
well tested. Enqueue the turn after the state transition and let a worker record
the resulting turn, artifact, and next state.

Deliverable: an API request no longer remains responsible for the lifetime of
that model process, and the turn recovers safely after worker termination.

### Phase 3: Research Reconciliation

Move resumable state advancement and bounded retry dispatch behind typed queue
tasks. Replace process-local background execution incrementally; do not rewrite
the research state machine at the same time.

Deliverable: restart-safe Honeydew/Beaker progress with queue-level concurrency,
priorities, cancellation, and operator visibility.

### Phase 4: Schedule Execution

Replace the stateless schedule-worker trigger mechanism with periodic queue
dispatch. Keep schedule definitions and authorization in `workflow-api` and
reuse its fail-closed due-execution path.

Deliverable: one standard worker mechanism for periodic digests and approved
reruns, with execution records still owned by `workflow-api`.

### Phase 5: Remove Superseded Machinery

Delete custom polling, retry, and worker lifecycle code only after each migrated
path has demonstrated restart recovery and equivalent audit behavior. Retain
domain reconciliation where external effects such as Kubernetes Jobs require
it.

Deliverable: smaller API and orchestrator services whose background mechanics
are supplied by the selected maintained queue stack.

## Testing Strategy

Each migration slice requires:

- unit tests for the handler's policy and idempotency behavior
- contract tests proving task payload compatibility
- integration tests with the real broker and Postgres
- worker-kill tests before, during, and after durable outcome recording
- duplicate-delivery tests for every external effect
- restart tests for broker, worker, API, and Postgres independently
- authorization tests proving unattended work cannot push, merge, deploy, or
  access secrets
- observability tests proving failed and stalled tasks are discoverable

Do not substitute mocks for the crash, redelivery, and restart integration
tests that decide whether the queue is trustworthy.

## Operational Requirements

- Queue infrastructure must use durable cluster storage before it becomes
  authoritative.
- Broker credentials must use the repository's SOPS workflow and must not enter
  task payloads.
- `.44` workers must run with bounded resources and process-tree cleanup.
- Queue depth, oldest-task age, failure count, retry count, and worker heartbeat
  must be observable.
- Operators need documented commands to pause intake, drain workers, inspect
  failures, retry one task, cancel pending work, and restore service.
- Queue and application records need explicit retention and backup policies.

## Non-Goals

- combining `research-orchestrator` and `workflow-api`
- replacing the research state machine with task-chain magic
- granting unattended deployment or GitHub mutation authority
- routing large artifacts through the broker
- treating NATS as authoritative merely because placeholder manifests exist
- migrating every background path in one release

## Success Criteria

The design succeeds when:

- API requests return after durable acceptance rather than owning long-running
  model processes
- accepted work survives service and worker restarts
- Honeydew and Beaker turns use explicit concurrency and routing policies
- operators can understand queued, active, retrying, failed, and completed work
  without reading process logs
- duplicate delivery cannot duplicate an externally visible effect
- scheduled operations and agent turns share maintained worker mechanics while
  retaining separate domain policies
- the repository contains less custom queue, retry, and lifecycle code than it
  did before the migration

