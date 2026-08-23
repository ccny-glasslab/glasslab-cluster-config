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
Upgrade only by resolving a new digest, testing it, and updating both the
manifest and this record.

## Availability boundary

One RabbitMQ node with a PVC. A single-member quorum queue is **persistent,
not highly available**: it survives pod restarts when the volume survives, but
it cannot tolerate node or PVC loss. Broker loss never loses durable state —
authoritative outboxes republish after reconstruction.

## Topology ownership

This directory owns the broker topology (`30-topology.yaml`). Applications do
not declare exchanges or queues; publishers and workers use the identities and
entities declared here. The topology carries a version marker
(`global_parameters` → `glasslab/topology-version`).

### Topology changes and incompatible declarations

The boot-time definitions import uses merge semantics: missing entities are
created, existing entities are left untouched, and argument changes to
existing entities are ignored (verified against RabbitMQ 4.3.5). There is
therefore no silent parallel-topology risk from client declarations either —
applications never declare topology, and publisher/consumer roles have no
configure permission.

Incompatible topology changes (type changes, argument changes, removals) MUST
introduce new entity names together with a `glasslab/topology-version` bump;
editing arguments of an existing name has no effect on the live broker. Old
queues drain naturally before retirement. Compatible additions may extend the
current version in place. The tracked template is pinned by
`tests/security/test_task_fabric_manifests.py`, so topology edits always
arrive through reviewed repository changes.

## Identities

Split least-privilege broker users are rendered at boot from the SOPS-managed
secret `glasslab-v2-rabbitmq` (see `10-secret.example.yaml`):

- `glasslab-topology-admin`: full administration (operators, not apps)
- `glasslab-publisher`: publish-only
- `glasslab-consumer`: consume plus retry/dead-letter republish
- `glasslab-monitor`: read-only monitoring

The management UI/API is loopback-only inside the pod and is not exposed by
the Service or NetworkPolicy. Administer with `kubectl exec ... -- rabbitmqctl`
or a port-forward from an operator session.

The boot-time definitions import also prevents creation of the default `guest`
user. Verify after deployment:

```bash
kubectl -n glasslab-v2 exec glasslab-rabbitmq-0 -c rabbitmq -- \
  rabbitmqctl list_users
```

## Exposure

- AMQP 5672 via ClusterIP Service `glasslab-rabbitmq`, ingress restricted by
  NetworkPolicy to research-orchestrator and workflow-api pods. Extend
  `50-network-policy.yaml` when dedicated publisher/worker Deployments land
  (plan Tasks 4-5) — their pods will need their own allowed labels.
- No NodePort, LoadBalancer, Ingress, or management port exposure.

## Recovery

- **Pod restart:** StatefulSet restarts the pod; the PVC reattaches and the
  broker database on the volume recovers. Quorum queue contents survive.
- **Total volume loss:** the broker database is gone and is NOT rebuilt from
  backup. Reconstruction is: redeploy the manifests, let the boot-time import
  recreate the declared topology, then let each service's authoritative
  PostgreSQL outbox republish unconfirmed deliveries. Durable task records in
  PostgreSQL define what work still exists; anything parked only in a lost
  DLQ is re-derived by replaying policy-checked commands by stable task ID,
  never by hand-editing rows or publishing arbitrary messages.
- **Erlang cookie rotation** invalidates the persisted node database: treat it
  as total volume loss and follow the reconstruction path above.

Rollout uses the tracked script target:

```bash
./scripts/rollout-research-services.sh --service rabbitmq
```

The script requires the `glasslab-v2-rabbitmq` secret and the
`glasslab-rabbitmq-data` PVC to exist before applying anything. The default
`all` bundle does not touch the broker.
