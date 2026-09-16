# Storage And State

Glasslab v2 is live, but its durable-storage story is still in the bring-up phase.

## Current posture

- the cluster has no `StorageClass`
- `glasslab-v2` now has explicit PVCs for `Postgres` and `MinIO`
- the cluster now also has a tracked NFS-backed RWX path for shared datasets and artifacts
- `workflow-api` session and stage metadata now live in Postgres rather than the JSON store on the artifacts share
- the Postgres manifest uses a pgvector-capable image so semantic indexes can
  live beside workflow metadata without introducing a separate vector database
- Postgres uses a static local PV/PVC on `node01`
- MinIO uses a static local PV/PVC on `node01`
- NATS uses an explicit static local PV/PVC on `node05`

This means the current live path is partially durable:

- `Postgres`: durable on local disk
- `workflow-api` records: durable through Postgres
- `MinIO`: durable on local disk
- `NATS`: durable on local disk

Live placement reference from the 2026-03-19 validation:

- `workflow-api` is now validated live on pull-based scheduling and is currently running on `node05`
- `Postgres` on `node01`
- `MinIO` on `node01`
- `NATS` on `node05`

Reference:

- `../live-state-2026-03-19.md`

## Intended storage strategy

The intended first durable v2 step is:

- keep the cluster-wide default `StorageClass` unset
- use explicit static local PV/PVC wiring for the first durable v2 services
- store large artifacts on the `.207` g-nas shared artifacts PVC instead of in
  Postgres or per-run PVCs
- use MinIO only where object-style access is deliberately needed
- revisit a shared CSI-backed default `StorageClass` only after the lab deliberately chooses and operates one

Current committed first step:

- `kubeadm/glasslab-v2/storage/10-static-local-pv.yaml` binds:
  - `glasslab-postgres-data` to `/var/lib/glasslab-v2/postgres` on `node01`
  - `glasslab-minio-data` to `/var/lib/glasslab-v2/minio` on `node01`
  - `glasslab-nats-data` to `/var/lib/glasslab-v2/nats` on `node05`
- `kubeadm/glasslab-v2/storage/20-nfs-static-pv.yaml` binds:
  - `glasslab-shared-datasets` to `192.168.1.207:/volume1/backup/glasslab-v2/shared-datasets`
  - `glasslab-shared-artifacts` to `192.168.1.207:/volume1/backup/glasslab-v2/shared-artifacts`

Live validation on 2026-03-19:

- both PVCs are bound
- `glasslab-postgres` restarted and retained a marker row across restart
- `glasslab-minio` restarted and retained a marker file across restart
- `./scripts/smoke-test-v2.sh` still passed after the cutover

Future storage placeholders live under `kubeadm/glasslab-v2/storage/`.

## Workload expectations

### Durable volumes required

- Postgres: durable PV required before treating run state as persistent
- shared artifacts PVC on `.207`: required before treating artifacts or reports
  as persistent
- MinIO: optional object-store layer, not the required first landing zone for
  large artifacts
- optional MLflow: durable PV required if enabled

### Ephemeral is acceptable for now

- `workflow-api`: stateless deployment with private GHCR image pulls via `glasslab-ghcr-pull`
- NATS: single-instance JetStream on retained local storage on `node05`

### Artifact direction

- workflow outputs should end up under `/mnt/artifacts/{run_id}` on the
  `.207`-backed shared artifacts PVC
- dataset snapshots may live in MinIO if needed later
- per-run Kubernetes PVCs are not the intended long-term artifact pattern

## Local PV versus shared storage

### Static local PVs

Advantages:

- matches the current cluster and the existing v1 operational pattern
- keeps node affinity explicit
- does not require operating a CSI stack immediately

Tradeoffs:

- service rescheduling is node-bound
- node loss requires operator restore work
- backups remain an operator responsibility

### Shared or CSI-backed storage

Advantages:

- easier pod mobility
- clearer future path for additional stateful services

Tradeoffs:

- introduces a new cluster primitive to operate
- should not be made the default until it is intentionally chosen, tested, and documented

### Shared network storage

This is now a real near-term option worth evaluating.

Good initial fits:

- shared datasets
- shared artifacts

Less attractive first fits:

- Postgres on casual network storage without careful validation
- MinIO on casual network storage without deciding the operational model first

Reference:

- `network-storage-integration.md`

## Expected internal DNS names

- `glasslab-workflow-api.glasslab-v2.svc.cluster.local`
- `glasslab-postgres.glasslab-v2.svc.cluster.local`
- `glasslab-nats.glasslab-v2.svc.cluster.local`
- `glasslab-minio.glasslab-v2.svc.cluster.local`
