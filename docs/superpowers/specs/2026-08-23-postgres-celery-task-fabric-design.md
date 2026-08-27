# PostgreSQL + Celery Task Fabric Design

**Status:** Approved direction; correctness review incorporated; implementation pending

## Decision

Use PostgreSQL as authoritative state, one transactional outbox per service
that owns a domain transaction, RabbitMQ as Celery's broker, and thin Celery
workers around typed service-local commands. PostgreSQL records—not Celery
results—remain the operator and recovery surface.

Pin the first tested implementation to Celery `5.6.3` and RabbitMQ `4.3.5`.
Manifests must use the tested RabbitMQ OCI digest, not a mutable version tag.
Do not use Celery's database transport, build a custom PostgreSQL queue, or
treat NATS as a second authoritative task broker.

## Transactional authorities

The orchestrator and workflow API have separate databases and no cross-service
transaction. Each owns its domain mutation, task intent, and outbox atomically:

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

A shared `task-fabric` package defines envelopes, claims, leases/fencing,
publisher behavior, and metrics. It does not own either domain database.

A standalone `enqueue_task_intent()` cannot prove atomicity. Domain-triggered
work uses service-specific unit-of-work methods that commit the domain mutation,
stable task intent, and outbox row together. Administrative replay is itself an
explicit domain transaction. Queue mode is PostgreSQL-only; SQLite remains a
development/import compatibility path and fails closed in queue mode.

## Invariants

1. Broker messages are delivery hints, never scientific truth.
2. Every task has a stable idempotency key in its authority's PostgreSQL store.
3. Domain state and outbox intent commit in one authority-local transaction.
4. Workers load current records and re-check policy before acting.
5. Acknowledgement occurs only after durable outcome commit.
6. Claims have an owner, attempt number, expiry, heartbeat, and fencing token.
7. Completion is compare-and-swap against the active fencing token.
8. Retries are bounded by task class and recorded with sanitized errors.
9. Approvals, legal transitions, cancellation, evaluation contracts, artifact
   verification, and Kubernetes policy remain deterministic Glasslab code.
10. Prompts, broker data, logs, and results contain no secrets or decrypted
    SOPS content.

## Durable records and claims

Each authority owns equivalent `task_intents`, `task_attempts`, and
`task_outbox` tables. Attempts record lease owner, fencing token, lease expiry,
heartbeat, attempt number, timestamps, and sanitized failure class. Outbox rows
have publisher claim owner/token/expiry and confirmation state.

Publishers claim bounded batches with expiring leases, publish identifier-only
envelopes, wait for confirms, and mark publication with compare-and-swap. Stale
publisher claims are reclaimable. A crash after publish but before marking can
duplicate delivery, which is expected.

A worker:

```text
delivery
  -> claim READY/RETRYABLE intent with lease + fencing token
  -> reload authority state and re-check policy
  -> heartbeat during long commands
  -> execute one typed command
  -> CAS completion with the same fencing token
  -> acknowledge delivery
```

A reaper marks expired attempts abandoned and creates a new outbox delivery
when the durable ceiling permits. An old worker cannot commit with a stale
token. A duplicate for an actively leased task is delayed/rejected, not treated
as completed.

Kubernetes uses deterministic Job names plus ownership-label verification.
Events and artifacts use database uniqueness constraints. No external side
effect is described as exactly once merely because task intent is idempotent.

## RabbitMQ and Celery contract

Use durable quorum queues, persistent messages, publisher confirms, explicit
acknowledgements, bounded prefetch, dead-letter exchanges, message TTL,
maximum length/bytes, and explicit overflow policy. Queue declarations are
versioned; incompatible declarations fail deployment rather than creating a
parallel topology.

Celery configuration includes:

- Celery `5.6.3`;
- explicit quorum queues and `task_default_queue_type = "quorum"`;
- `broker_transport_options = {"confirm_publish": true}`;
- quorum detection enabled, with no global-QoS or autoscaling assumptions;
- native delayed delivery tested for countdowns;
- late acknowledgement and worker-loss rejection;
- result backend disabled;
- explicit routes and no production auto-creation of missing queues.

Initial queues are `glasslab.orchestrator.control` and
`glasslab.workflow.execution`, with corresponding retry/dead-letter queues.
`glasslab.agent` is deferred until Hermes command ownership is extracted.

Celery retry exhaustion does not automatically implement Glasslab dead-letter
semantics. On classified terminal/exhausted outcomes, the adapter commits
`FAILED` or `DEAD_LETTERED`, explicitly publishes a sanitized DLQ envelope with
confirmation, then acknowledges the original. Malformed/unsupported envelopes
are rejected to the DLX without domain execution and create an operator event
when a valid task ID is available.

## Discord semantics

Discord has no caller-supplied idempotency key, so projection delivery is
at-least-once. The durable projection record stores content identity and the
Discord channel/message ID after success. Retries update a known message when
possible. A crash after Discord accepts a new send but before the ID commits
can duplicate it. Messages carry a stable non-secret run/event marker so
reconciliation and operators can identify duplicates. This never changes
authoritative research state, and zero duplicate Discord sends is not an
acceptance criterion.

## Packaging and deployment

`services/task-fabric` is a versioned internal package containing shared
envelopes, lease/fencing primitives, broker configuration, and adapters.
Service-local commands and stores remain with their owner; workers do not
import across unrelated application trees.

Each authority gets its own publisher Deployment and least-privilege database
role. Worker roles are command-family-specific. Broker users are split among
topology administration, publishing, consuming, and read-only monitoring. The
management UI is disabled.

Database migrations precede publishers/workers. Old versions ignore additive
tables. Publishers stay disabled until readers understand the envelope schema.
Rollback disables enqueue/publish flags, drains or parks deliveries, and
restores synchronous handling without deleting durable task records.

## Migration scope

1. Orchestrator reconciliation and artifact ingestion.
2. Orchestrator projection preparation and at-least-once Discord delivery.
3. Workflow API submission/observation only after it owns its own task/outbox
   transaction and crash-boundary tests pass.

The current `schedule-worker` stays a stateless workflow-api HTTP caller until
that workflow-local boundary exists. It never writes the orchestrator outbox.
Approvals, transition decisions, Hermes sessions, evaluation scoring, and
cancellation truth remain synchronous/deterministic.

## Recovery

- Publisher dies before publish: claim expires and is retried.
- Publisher dies after publish before marking: duplicate delivery is safe.
- Worker dies before commit: broker redelivery or stale-lease reaping resumes.
- Worker dies after commit before ack: redelivery observes terminal state.
- Worker loses its lease: stale fencing token cannot commit.
- Broker outage: domain/outbox commits continue and publishing resumes later.
- PostgreSQL outage: workers do not acknowledge or invent outcomes.
- Poison/malformed delivery: explicit DLX plus durable operator state.
- Discord ambiguity: bounded at-least-once projection with stable markers.

## Availability boundary

The first deployment is one RabbitMQ node with a PVC. A single-member quorum
queue is durable across pod restart when its volume survives, but it is not
highly available and cannot tolerate node/PVC loss. PostgreSQL remains
authoritative, so outboxes can republish after broker reconstruction. Test pod
restart, broker database recovery, PVC reattachment, and total broker-loss
reconstruction.

Horizontal orchestrator replicas remain unsupported until run leadership or
claim semantics land. Multiple task workers/publishers are safe only through
the lease/fencing protocol.

## Operations and acceptance

Expose queue depth/age, outbox age, expired claims, heartbeat age, publish
errors, worker state, task latency, retries, dead letters, and intent state.
Operators replay or abandon by stable task ID through policy-checked commands,
never manual row edits or arbitrary broker publication.

Acceptance requires:

- atomic mutation plus enqueue within each owning service;
- no duplicate Kubernetes Jobs, database events, or artifacts;
- explicit detectable at-least-once Discord delivery;
- recovery of expired worker/publisher claims and fencing of stale owners;
- crash-boundary convergence tests;
- tested durable terminal and DLQ behavior for exhausted/malformed work;
- reconstruction after queue loss;
- documented/tested single-node and PVC-loss limitations;
- staged synchronous fallback throughout migration.

## Rejected alternatives

- **One cross-service outbox:** cannot cover independent databases atomically.
- **Custom PostgreSQL queue:** retains lease, routing, retry, and supervision
  machinery.
- **Celery SQLAlchemy broker:** weaker than the intended broker model.
- **NATS Celery broker:** no mature first-class transport.
- **Broker as truth:** loses transactional scientific state.
- **Exactly-once Discord:** unsupported by Discord's send API.
