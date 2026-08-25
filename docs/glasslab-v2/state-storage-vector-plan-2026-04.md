# State, Storage, And Vector Plan 2026-04

This is the current plan for unifying Glasslab state after the metric-search
storage issue.

## Decision

Postgres is the system of record for workflow state and semantic indexes.

The g-nas export on `.207` is the system of record for large datasets and large
run artifacts.

MinIO remains optional infrastructure for object-style access, but it is not
where large Glasslab artifacts must land first. The `.207` NFS-backed PVCs are
the concrete storage plane that jobs and `workflow-api` can both reach today.

## Canonical Stores

| Data | Canonical store | Notes |
| --- | --- | --- |
| Sessions, intakes, plans, runs, comparisons, decisions | Postgres | Owned by `workflow-api` |
| Search/vector metadata | Postgres with pgvector | Stores embeddings and pointers, not blobs |
| Dataset files | g-nas `.207` via `glasslab-shared-datasets` | Mounted read-only by jobs where possible |
| Run artifact bundles | g-nas `.207` via `glasslab-shared-artifacts` | Jobs write bundles under a run-specific directory |
| Logs and reports | g-nas `.207` via `glasslab-shared-artifacts` | Referenced from Postgres run records |
| Source PDFs/HTML snapshots | g-nas `.207` now; MinIO optional later | Keep references in Postgres |
| Scratch/cache | Pod-local or node-local ephemeral | Never canonical |

## Current Manifest Direction

The committed v2 manifests now point at this direction:

- `workflow-api` uses `GLASSLAB_WORKFLOW_API_STORE_BACKEND=postgres`.
- Postgres uses the `pgvector/pgvector:pg16` image.
- The shared datasets PVC is backed by
  `192.168.1.207:/volume1/backup/glasslab-v2/shared-datasets`.
- The shared artifacts PVC is backed by
  `192.168.1.207:/volume1/backup/glasslab-v2/shared-artifacts`.

The old JSON store path remains configured only as a migration/import reference:

```text
/mnt/artifacts/workflow-api/state/run-store.json
```

It should not be treated as live workflow state after the Postgres cutover.

## Vector Store Shape

Use the same Postgres instance for v0 vector search. This keeps backups,
authorization, and operational debugging in one stateful service.

The initial table is `vector_index_items`:

- `item_id`: stable item ID
- `collection`: logical index such as `source-documents`, `run-artifacts`, or
  `techniques`
- `owner_type` and `owner_id`: owning workflow object
- `text_hash`: dedupe key for embedded text
- `embedding vector(1536)`: initial embedding size
- `payload`: JSON metadata for chunk boundaries, model name, scores, etc.
- `artifact_uri`: pointer to the large file or chunk source on `.207`

Do not store PDFs, checkpoints, images, notebooks, or metrics blobs in the
vector table.

## Run Artifact Contract

Workload Jobs write completed bundles to:

```text
/mnt/artifacts/{run_id}/
```

Because `/mnt/artifacts` is `glasslab-shared-artifacts`, this means the bytes
land on:

```text
192.168.1.207:/volume1/backup/glasslab-v2/shared-artifacts/{run_id}/
```

`workflow-api` should ingest the bundle metadata into Postgres:

- terminal status
- metrics summary
- artifact index
- file references under the shared artifact root

The database record should store paths/URIs, not file contents.

## Cutover Plan

1. Confirm the live `.44` secret `glasslab-v2-workflow-api` contains
   `GLASSLAB_WORKFLOW_API_STORE_POSTGRES_DSN`.
2. Import the existing JSON store once:

   ```bash
   python3 services/workflow-api/scripts/import-json-store-to-postgres.py \
     --json-path /mnt/artifacts/workflow-api/state/run-store.json
   ```

   The importer reads the DSN from
   `GLASSLAB_WORKFLOW_API_STORE_POSTGRES_DSN`. For a protected file or pipe,
   pass an already-open descriptor with `--dsn-fd` instead. The legacy
   secret-bearing `--dsn` argument is rejected.

3. Apply the Postgres pgvector image and `workflow-api` ConfigMap updates.
4. Restart `workflow-api`.
5. Verify `/healthz` reports `store_backend: postgres`.
6. Create a test run and confirm:
   - the run record appears after a pod restart
   - the artifact bundle exists under the `.207` shared artifacts export
   - only references/summaries are stored in Postgres

## Non-Goals

- Do not move large artifacts into Postgres.
- Do not make MinIO mandatory for GPU run completion.
- Do not keep JSON-on-NFS as the live workflow database.
- Do not introduce a separate vector database until pgvector becomes a measured
  bottleneck.
