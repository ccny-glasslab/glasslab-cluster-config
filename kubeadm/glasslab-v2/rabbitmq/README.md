# glasslab-rabbitmq (task-fabric broker)

Single-node RabbitMQ 4.3.5 for the PostgreSQL + Celery task fabric. The broker
is delivery infrastructure only: PostgreSQL remains authoritative for all task
state, outboxes, attempts, leases, and results
([ADR 0004](../../docs/glasslab-v2/adr/0004-task-fabric-authorities.md)).

## Tested image identity

Resolved 2026-08-23 from `docker.io/library/rabbitmq:4.3.5`:

| Artifact | Digest |
| --- | --- |
| linux/amd64 manifest (pinned in `60-statefulset.yaml`) | `sha256:cb038b7a48d8b73507c83ff446245546a9459ac53e9ce79615217b4fbd917d50` |
| multi-arch index | `sha256:9d39258795e314bec0a204db15cc0b8770590ae983d88efacf159c766b1e539d` |

The definitions renderer pins `python:3.11-slim`
(`sha256:9c900dea9e8fb7e16277c179b555cc72d29a352dbc33cff48ad5a0412fd5bfc7`).
Upgrade only by resolving a new digest, re-running the runtime tests against
it, and updating both the manifest and this record.

## Runtime verification

`tests/integration/test_task_fabric_broker_runtime.py` boots the pinned image
with the tracked manifests and demonstrates, reproducibly:

- boot-time definitions import with **zero plugins enabled**;
- the declared topology (quorum queues, arguments, users) and absence of the
  `guest` user;
- merge semantics of re-import (existing queue arguments are left unchanged)
  and that the drift check fails loudly on such changes;
- credential rotation via secret change + restart (re-import upserts user
  passwords);
- erlang cookie rotation preserving persistent broker metadata;
- topology persistence across a plain restart.

Run locally with Docker available:

```bash
pytest -q tests/integration
```

## Availability boundary

One RabbitMQ node with a PVC. A single-member quorum queue is **persistent,
not highly available**: it survives pod restarts when the volume survives, but
it cannot tolerate node or PVC loss. Broker loss never loses durable state —
authoritative outboxes republish after reconstruction.

## Management surface

The management plugin is not enabled (`enabled_plugins` is `[]`); the
management UI/API does not exist in the pod. Boot-time local-file definitions
import is core RabbitMQ functionality
(`rabbit_definitions_import_local_filesystem` ships inside the `rabbit`
application itself — verified against the pinned image), so no plugin is
required for it. Administration uses `kubectl exec ... -- rabbitmqctl`.

## Topology ownership and drift enforcement

This directory owns the broker topology (`30-topology.yaml`). Applications do
not declare exchanges or queues; publishers and workers use the identities and
entities declared here. The topology carries a version marker
(`global_parameters` → `glasslab/topology-version`, currently `1`).

### Merge semantics of the boot import

The boot-time definitions import uses merge semantics: missing entities are
created; existing entities are left untouched, including their arguments
(verified). Editing an argument of an existing entity therefore has **no
effect** on the live broker.

### Deterministic drift detection

Because merge semantics can hide divergence between Git and the running
broker, every broker start runs a conformance check
(`verify-topology.sh` via a postStart hook): it exports the live definitions
and compares them against the rendered expected file — vhost, topology
version, queues (flags plus exact declared arguments), exchanges (type and
flags), bindings, per-user permissions, and user existence. Any mismatch
raises and gives the hook a non-zero exit, which kills the container: drift
surfaces as a CrashLoopBackOff and `rollout-research-services.sh` stops at
`rollout status` instead of continuing silently.

Incompatible topology changes MUST therefore introduce new entity names
together with a `glasslab/topology-version` bump; old queues drain naturally
before retirement. Compatible additions may extend the current version in
place. Password hashes are deliberately excluded from this comparison — see
rotation below.

## Identities and credential rotation

Split least-privilege broker users are rendered at boot from the SOPS-managed
secret `glasslab-v2-rabbitmq` (see `10-secret.example.yaml`):

- `glasslab-topology-admin`: full administration (operators, not apps)
- `glasslab-publisher`: publish-only
- `glasslab-consumer`: consume plus retry/dead-letter republish
- `glasslab-monitor`: read-only monitoring

The default `guest` user is never created when definitions are imported at
boot (verified).

**Rotation procedure (tested):** the boot-time re-import upserts existing
users' passwords from the rendered file. To rotate:

1. Update the passwords in the SOPS-managed `glasslab-v2-rabbitmq` secret.
2. Restart the broker pod (`kubectl -n glasslab-v2 rollout restart
   statefulset/glasslab-rabbitmq`). The init container re-renders definitions;
   the boot import updates the existing users' passwords.
3. Restart the publisher/worker Deployments that hold the old credentials in
   their environment.

There is no need to delete users or wipe data when rotating passwords.
Erlang cookie rotation is different — see below.

## Erlang cookie

The cookie authenticates Erlang distribution (CLI tools talking to the node).
It is not entangled with persistent broker metadata: rotating the cookie while
the volume persists leaves queues, messages, and users fully intact
(demonstrated by the runtime tests). Because the init container rewrites the
cookie file on every boot from the secret, rotation is simply: update the
`erlang_cookie` secret key and restart the pod. In-pod CLI tooling keeps
working; any external CLI tooling must be updated to the new cookie.

## Exposure

- AMQP 5672 via ClusterIP Service `glasslab-rabbitmq`, ingress restricted by
  NetworkPolicy to research-orchestrator and workflow-api pods. Extend
  `50-network-policy.yaml` when dedicated publisher/worker Deployments land
  (plan Tasks 4-5) — their pods will need their own allowed labels.
- No NodePort, LoadBalancer, Ingress, or management port exposure.

## Recovery

- **Pod restart:** StatefulSet restarts the pod; the PVC reattaches and the
  broker database on the volume recovers. Quorum queue contents survive.
  Topology conformance is re-checked at every start.
- **Total volume loss:** the broker database is gone and is NOT rebuilt from
  backup. Reconstruction is: redeploy the manifests, let the boot-time import
  recreate the declared topology, then let each service's authoritative
  PostgreSQL outbox republish unconfirmed deliveries. Durable task records in
  PostgreSQL define what work still exists; anything parked only in a lost
  DLQ is re-derived by replaying policy-checked commands by stable task ID,
  never by hand-editing rows or publishing arbitrary messages.

Rollout uses the tracked script target:

```bash
./scripts/rollout-research-services.sh --service rabbitmq
```

The script requires the `glasslab-v2-rabbitmq` secret and the
`glasslab-rabbitmq-data` PVC to exist before applying anything. The default
`all` bundle does not touch the broker.
