# PostgreSQL + Celery Task Fabric Implementation Plan

> Execute one task at a time with review checkpoints. Do not parallelize tasks
> that touch the same store, migration, or deployment authority.

**Goal:** Add a RabbitMQ/Celery delivery fabric driven by authority-local
PostgreSQL outboxes without moving authoritative Glasslab state into the broker.

**Pinned baseline:** Python 3.11, Celery 5.6.3, RabbitMQ 4.3.5 (tested OCI
digest selected in Task 2), PostgreSQL, psycopg, FastAPI, Kubernetes, SOPS,
pytest.

**Spec:** `docs/superpowers/specs/2026-08-23-postgres-celery-task-fabric-design.md`

## Global constraints

- The orchestrator and workflow API each own their transaction/outbox.
- Domain mutation plus enqueue uses one authority-local unit of work.
- Payloads contain identifiers and schema version only.
- Claims and publisher locks have leases and fencing tokens.
- Result backend is disabled; PostgreSQL is authoritative.
- Discord is at-least-once; Kubernetes/events/artifacts use side-effect-specific
  idempotency.
- Queue mode is PostgreSQL-only and remains feature-flagged until fault tests
  pass.
- Credentials never enter Git, argv, logs, broker payloads, or snapshots.

---

### Task 1: Shared task-fabric protocol and authority boundaries

**Files:**
- Create: `services/task-fabric/pyproject.toml`
- Create: `services/task-fabric/task_fabric/envelope.py`
- Create: `services/task-fabric/task_fabric/claims.py`
- Create: `services/task-fabric/tests/test_protocol.py`
- Create: `docs/glasslab-v2/adr/0004-task-fabric-authorities.md`

- [ ] Write failing tests for versioned identifier-only envelopes, sanitized
  failure classes, lease expiry, fencing-token ordering, and invalid payloads.
- [ ] Define the service-local transaction rule and prohibit cross-service
  repository imports in the ADR.
- [ ] Implement only shared value objects/protocols; no database ownership.
- [ ] Pin package dependencies and make both service images install the package
  from the checked-out source.
- [ ] Run protocol tests and import-boundary tests.

### Task 2: RabbitMQ 4.3.5 topology and recovery boundary

**Files:**
- Create: `kubeadm/glasslab-v2/rabbitmq/`
- Create: `kubeadm/glasslab-v2/rabbitmq/10-secret.example.yaml`
- Create: `tests/security/test_task_fabric_manifests.py`
- Modify: `scripts/rollout-research-services.sh`

- [ ] Resolve and record the tested RabbitMQ 4.3.5 OCI digest.
- [ ] Write RED tests for digest pinning, PVC, non-root context, internal-only
  service, NetworkPolicy, SOPS refs, split broker identities, and disabled UI.
- [ ] Declare versioned exchanges; orchestrator/workflow quorum queues; retry
  and DLQs; TTL; max length/bytes; overflow policy; resource alarms.
- [ ] Define incompatible-declaration failure behavior and topology ownership.
- [ ] Prove single-node pod restart, PVC reattachment, broker database recovery,
  and documented total-volume-loss reconstruction.
- [ ] State explicitly that one-node quorum is persistent, not highly available.

### Task 3: Orchestrator intent/outbox unit of work

**Files:**
- Create: `services/research-orchestrator/app/task_fabric_store.py`
- Create: `services/research-orchestrator/tests/test_task_fabric_store.py`
- Modify: `services/research-orchestrator/app/research_store.py`
- Modify: `services/research-orchestrator/app/postgres_store.py`
- Modify: `services/research-orchestrator/app/storage.py`

- [ ] Write PostgreSQL RED tests for atomic domain mutation + task intent +
  outbox creation; duplicate stable keys; attempts; lease/heartbeat expiry;
  fencing; CAS completion; and publisher claim expiry.
- [ ] Add additive migrations for `task_intents`, `task_attempts`, and
  `task_outbox`, including uniqueness and transition constraints.
- [ ] Add domain-specific atomic methods; do not expose standalone enqueue for
  normal domain work.
- [ ] Make SQLite reject queue mode while retaining synchronous compatibility.
- [ ] Test concurrent claims, stale worker completion, stale publisher marking,
  restart recovery, and migration rollback compatibility.

### Task 4: Authority-local outbox publisher Deployments

**Files:**
- Create: `services/task-fabric/task_fabric/publisher.py`
- Create: `services/task-fabric/tests/test_publisher.py`
- Create: `services/research-orchestrator/app/outbox_publisher.py`
- Create: `kubeadm/glasslab-v2/task-fabric/orchestrator-publisher.yaml`
- Modify: `.github/workflows/manual-docker.yml`

- [ ] Write RED tests for leased batches, confirmation-before-marking, publish
  failure, crash-after-publish duplication, stale claim takeover, and redaction.
- [ ] Implement shared publisher mechanics with a service-local store adapter.
- [ ] Add the orchestrator publisher Deployment, probes, metrics, feature flag,
  and least-privilege DB/broker identities.
- [ ] Test mixed-version state: migrated schema with old app, disabled new
  publisher, enabled publisher, and rollback to synchronous operation.

### Task 5: Celery 5.6.3 worker, fencing, retry, and DLQ semantics

**Files:**
- Create: `services/task-fabric/task_fabric/celery_app.py`
- Create: `services/task-fabric/task_fabric/worker.py`
- Create: `services/task-fabric/tests/test_worker.py`
- Create: `kubeadm/glasslab-v2/task-fabric/orchestrator-worker.yaml`

- [ ] Write RED tests for explicit quorum queues, confirm publish, late ack,
  worker-loss reject, result backend disabled, explicit routes, prefetch, and no
  implicit production queues.
- [ ] Test Celery 5.6.3 quorum behavior in a real RabbitMQ 4.3.5 integration
  environment, including per-channel QoS and native delayed delivery.
- [ ] Implement lease claim, heartbeat, stale-lease reaper, fencing-token CAS,
  bounded durable attempt ceilings, and terminal no-op redelivery.
- [ ] Define retry envelope fields and the single source of attempt truth.
- [ ] Implement explicit `FAILED`/`DEAD_LETTERED` commit plus confirmed DLQ
  publication; test malformed and exhausted messages end-to-end.
- [ ] Add queue declaration mismatch and broker restart integration tests.

### Task 6: Migrate orchestrator-local background commands

**Files:**
- Create: `services/research-orchestrator/app/background_commands.py`
- Modify: `services/research-orchestrator/app/engine.py`
- Modify: `services/research-orchestrator/app/discord_adapter.py`
- Create: `services/research-orchestrator/tests/test_background_commands.py`

- [ ] Extract typed reconciliation, artifact-ingestion, projection-preparation,
  and projection-delivery commands while retaining synchronous calls.
- [ ] Prove synchronous and queued handlers produce identical durable outcomes.
- [ ] Add atomic command-specific enqueue methods behind disabled flags.
- [ ] Test cancellation, duplicate delivery, restart, expired lease, and broker
  outage behavior.
- [ ] For Discord, persist content identity/message ID, update known messages,
  attach stable markers, and test the crash ambiguity as detectable
  at-least-once delivery—not impossible exactly-once delivery.
- [ ] Enable one command class at a time in integration.

### Task 7: Workflow API authority-local execution queue

**Files:**
- Create: `services/workflow-api/app/task_fabric_store.py`
- Create: `services/workflow-api/app/outbox_publisher.py`
- Create: `services/workflow-api/app/execution_commands.py`
- Create: `services/workflow-api/tests/test_task_fabric_store.py`
- Create: `services/workflow-api/tests/test_execution_commands.py`
- Create: `kubeadm/glasslab-v2/task-fabric/workflow-publisher.yaml`
- Create: `kubeadm/glasslab-v2/task-fabric/workflow-worker.yaml`
- Modify: `services/workflow-api/app/execution_routes.py`

- [ ] Add workflow-local intent/attempt/outbox migrations and the same tested
  lease/fencing contract; do not consume the orchestrator outbox.
- [ ] Atomically commit workflow run/job state plus execution intent/outbox.
- [ ] Add workflow publisher/worker Deployments and least-privilege roles.
- [ ] Keep `schedule-worker` as a stateless HTTP caller; remove no behavior
  until this boundary is live.
- [ ] Use deterministic Job names and ownership labels, plus event/artifact DB
  uniqueness.
- [ ] Fault-test API/publisher/worker crashes before and after Job creation,
  stale observations, cancellation races, duplicates, and broker outage.
- [ ] Enable async submission only after synchronous/queued parity and mixed-
  version rollback tests pass.

### Task 8: Operations, replay, rollout, and reconstruction

**Files:**
- Create: `scripts/task-fabric-status.sh`
- Create: `scripts/task-fabric-replay.sh`
- Create: `docs/glasslab-v2/runbooks/task-fabric.md`
- Modify: `scripts/check-before-push.sh`
- Modify: `scripts/smoke-test-v2.sh`

- [ ] Write boundary tests keeping DSNs and broker credentials out of argv and
  output.
- [ ] Report queue/outbox age, lease expiry/heartbeats, intent attempts,
  publisher errors, and DLQs without secrets.
- [ ] Implement policy-checked replay/abandon by stable task ID; never require
  row editing or arbitrary message publication.
- [ ] Test PostgreSQL outage, broker outage, worker/publisher kill, malformed
  envelopes, retry exhaustion, declaration mismatch, PVC restart, and total
  broker reconstruction from authoritative outboxes/intents.
- [ ] Document SOPS enrollment, migration ordering, image digests, feature
  flags, queue drain/parking, rollback, and NATS coexistence/removal.
- [ ] Run the full repository gate and an independent security review before
  enabling production queue mode.

## Deferred work

Hermes turns and `glasslab.agent` remain deferred until agent-session behavior
is extracted from `ResearchOrchestrator` and has deterministic idempotency,
lease, cancellation, and recovery tests.
