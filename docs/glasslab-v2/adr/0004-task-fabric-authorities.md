# ADR 0004: Task Fabric Authority Boundaries

Status: accepted

Sources:
[PostgreSQL + Celery task fabric design](../../superpowers/specs/2026-08-23-postgres-celery-task-fabric-design.md)
and
[implementation plan](../../superpowers/plans/2026-08-23-postgres-celery-task-fabric.md),
merged via [#178](https://github.com/ccny-glasslab/glasslab-cluster-config/pull/178).

## Decision

Glasslab adds a RabbitMQ/Celery delivery fabric ("task fabric") for background
work. The fabric changes how work is delivered; it does not change what is
authoritative.

### PostgreSQL is authoritative

PostgreSQL remains the single authority for:

- domain state (runs, events, approvals, jobs, artifacts, contracts);
- task state (`task_intents` and their legal transitions);
- transactional outboxes;
- attempt records, including lease owner, fencing token, lease expiry,
  heartbeat, and attempt number;
- results and terminal outcomes;
- audit history.

RabbitMQ and Celery are delivery infrastructure only. Broker messages are
delivery hints, never scientific truth. Losing broker data must never lose
durable state: outboxes can republish after total broker reconstruction.
Celery's result backend is disabled; PostgreSQL records are the operator and
recovery surface.

### One transactional outbox per authority boundary

research-orchestrator and workflow-api have separate databases and no
cross-service transaction. Each owns a local transactional outbox inside its
own authority boundary:

```text
orchestrator transaction               workflow-api transaction
  run/event mutation                     run/job mutation
  orchestrator task_intent               workflow task_intent
  orchestrator task_outbox               workflow task_outbox
          |                                      |
          v                                      v
  orchestrator publisher                 workflow publisher
          +-------------- RabbitMQ --------------+
```

Domain mutation and outbox insertion occur in one database transaction within
the owning service. A standalone `enqueue_task_intent()` cannot prove
atomicity, so domain-triggered work uses service-specific unit-of-work methods.
Administrative replay is itself an explicit domain transaction. Queue mode is
PostgreSQL-only; SQLite fails closed in queue mode.

The shared `services/task-fabric` package defines envelopes, claims, leases,
fencing, publisher behavior, and metrics. It owns no database and no domain
state. Service-local commands and stores remain with their owner; workers do
not import across unrelated application trees.

### Leased, fenced claims

Worker and publisher claims carry an owner, attempt number, expiry, heartbeat,
and monotonically increasing fencing token. Completion is compare-and-swap
against the active fencing token, so a stale worker or stale publisher cannot
commit after its claim expires or is taken over. A crash between publish and
marking can duplicate delivery; this is expected and safe because consumers are
idempotent against authoritative state.

### Discord delivery is at-least-once

Discord has no caller-supplied idempotency key, so projection delivery is
at-least-once. Durable projection records store content identity and the
Discord message ID after success; retries update a known message when possible.
Messages carry a stable non-secret run/event marker so duplicates are
detectable. Consumers of every delivered task must be idempotent; zero
duplicate Discord sends is not an acceptance criterion.

### The schedule-worker stays a stateless HTTP caller

The current `schedule-worker` remains a stateless workflow-api HTTP caller. It
never writes the orchestrator outbox and gains no queue authority until a
workflow-local boundary exists (plan Task 7).

### Single-node RabbitMQ is persistence, not high availability

The first deployment is one RabbitMQ node with a PVC. A single-member quorum
queue survives pod restart when its volume survives, but it is not highly
available and cannot tolerate node or PVC loss. This limitation is documented
and tested rather than hidden; PostgreSQL authority is what makes broker loss
recoverable.

## Consequences

- Approvals, legal transitions, cancellation, evaluation contracts, artifact
  verification, and Kubernetes policy remain deterministic Glasslab code.
- Credentials never enter Git, argv, logs, broker payloads, or snapshots.
  Envelopes carry identifiers and schema version only — never payload bodies,
  prompts, secrets, or raw exception text.
- Rollback disables enqueue/publish flags and restores synchronous handling
  without deleting durable task records.
