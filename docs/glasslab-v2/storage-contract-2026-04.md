# Storage Contract 2026-04

This document is the concrete storage boundary for Glasslab v2.

It replaces the informal pattern where Postgres, shared PVCs, JSON files, and
MinIO were treated as interchangeable fallback layers.

## Design Rule

Postgres owns records and v0 vector indexes.

The g-nas export on `.207` owns durable large datasets, run artifacts, reports,
logs, and source-document blobs through the shared NFS PVCs.

MinIO is optional object-store infrastructure, not the required first landing
zone for large Glasslab artifacts.

Pod-local filesystems own scratch only.

No component should use a file/object store as the system of record for workflow
state, and no component should treat Postgres as a blob store.

## Storage Classes By Data Type

| Data type | Canonical store | Current bridge | Owner |
| --- | --- | --- | --- |
| Sessions | Postgres | none | `workflow-api` |
| Designs, decisions, campaigns | Postgres | none | `workflow-api` |
| Run records | Postgres | none | `workflow-api` |
| Comparison records | Postgres | none | `workflow-api` |
| Vector/search index entries | Postgres with pgvector | none | `workflow-api` |
| Dataset files | `.207` g-nas via shared dataset PVC | optional MinIO mirror later | operator/dataset sync |
| Source-document blobs | `.207` g-nas via shared artifacts PVC | optional MinIO mirror later | `workflow-api` |
| Run artifact bundles | `.207` g-nas via shared artifacts PVC | none | workload runner + `workflow-api` |
| Reports and comparison files | `.207` g-nas via shared artifacts PVC | optional MinIO mirror later | reporter/evaluator |
| Logs | `.207` g-nas for durable logs, pod logs for live debugging | none | runner |
| Secrets | Kubernetes Secrets plus encrypted `.44` backup | local `.44` manifests | operator |
| Cache/scratch | pod-local or node-local ephemeral storage | none | individual workload |

## Buckets

Use a small fixed bucket set only when object-style access is needed.

The required large-artifact path is the shared artifacts PVC backed by:

```text
192.168.1.207:/volume1/backup/glasslab-v2/shared-artifacts
```

| Bucket | Purpose |
| --- | --- |
| `glasslab-run-artifacts` | optional mirror for per-run artifact bundles |
| `glasslab-source-documents` | optional mirror for fetched PDFs, HTML snapshots, and source blobs |
| `glasslab-comparisons` | optional mirror for comparison reports, summaries, and evaluator outputs |

Do not put workflow/session state in these buckets.

## Object Key Layout

Run artifacts:

```text
s3://glasslab-run-artifacts/runs/{run_id}/run_manifest.json
s3://glasslab-run-artifacts/runs/{run_id}/config.json
s3://glasslab-run-artifacts/runs/{run_id}/metrics.json
s3://glasslab-run-artifacts/runs/{run_id}/artifacts_index.json
s3://glasslab-run-artifacts/runs/{run_id}/report.md
s3://glasslab-run-artifacts/runs/{run_id}/status.json
s3://glasslab-run-artifacts/runs/{run_id}/logs/runner.log
```

Source documents:

```text
s3://glasslab-source-documents/sources/{document_id}/source.pdf
s3://glasslab-source-documents/sources/{document_id}/metadata.json
```

Comparisons:

```text
s3://glasslab-comparisons/comparisons/{comparison_id}/comparison.json
s3://glasslab-comparisons/comparisons/{comparison_id}/summary.md
```

## Record References

Postgres records should store object references, not object bytes.

Examples:

```json
{
  "run_id": "abc123",
  "artifact_refs": {
    "metrics": "s3://glasslab-run-artifacts/runs/abc123/metrics.json",
    "report": "s3://glasslab-run-artifacts/runs/abc123/report.md",
    "logs": "s3://glasslab-run-artifacts/runs/abc123/logs/"
  }
}
```

```json
{
  "comparison_id": "cmp123",
  "artifact_refs": {
    "comparison": "s3://glasslab-comparisons/comparisons/cmp123/comparison.json",
    "summary": "s3://glasslab-comparisons/comparisons/cmp123/summary.md"
  }
}
```

## Near-Term Execution Model

For Kubernetes Jobs, keep the shared artifacts PVC as the durable artifact plane.

The runner writes to:

```text
/mnt/artifacts/{run_id}/
```

That path is backed by the `.207` g-nas export. `workflow-api` records the
resulting file references and summaries in Postgres.

If a later MinIO mirror/promotion step is added, it should be idempotent:

- if the object already exists with the same content, keep it
- if a required file is missing, fail promotion explicitly
- if upload succeeds but Postgres update fails, retry using the same keys

## Run Artifact Isolation And Identity Binding

Every runner Job mounts only its own run directory under `/mnt/artifacts`:

- `services/workflow-api/app/job_submission.py` mounts `artifacts-volume` at
  `/mnt/artifacts/<run_id>` with `subPath=<run_id>` for every runner type
  (research-workspace and generic/tabular), and it creates that directory before
  the Job is submitted because the kubelet cannot mount a missing `subPath`.
- Read-only inputs (dataset bindings, research-workspace assets) are separate
  `subPath` mounts, so a workload can read approved inputs but cannot reach a
  sibling run's directory.

Status and index reads bind the on-disk evidence to the run id:

- `services/workflow-api/app/run_artifacts.py` rejects a `status.json` or
  `artifacts_index.json` whose embedded `run_id` is missing or names a different
  run (`load_status_from_disk`, `load_artifacts_from_disk`), matching the check
  already present in `load_terminal_bundle`.

### Same-run forgery: scope decision

Cross-run forgery is eliminated: a workload cannot write another run's evidence.
Same-run forgery — a workload writing a forged `status.json`/`metrics.json` for
its **own** run — remains possible and is **out of scope for this change**, as a
deliberate, documented decision.

Rationale and tradeoff:

- The run directory is the workload's own output area by design; the workload is
  the evidence producer. Closing same-run forgery requires separating the
  evidence writer from the workload (a trusted sidecar or an evidence-writer uid
  the workload cannot impersonate), plus filesystem ownership changes. That is a
  project-level architecture change, not a mount-scope fix.
- The controls that already bound same-run trust are the immutable evaluation
  contract and its digest verification, the authoritative durable run/artifact
  records in Postgres, and the orchestrator's reconciliation of live Kubernetes
  status. A forged status cannot mint a passing evaluation.
- Putting evidence-writer/uid separation in scope here would have broadened the
  change well beyond the flagged cross-run escalation and risked breaking how
  every runner writes its own terminal status.

Follow-up: track evidence-writer/container separation as its own issue. Until
then, same-run integrity is an evaluation-contract responsibility, not a
filesystem-permission guarantee.

## Object-Store Credential Scoping

The root object-store credential is administrative. It is consumed only by the
MinIO server and the one-shot provisioning Job, and every other service uses a
bucket-scoped user. This preserves per-caller attribution and bounds a pod
compromise to that caller's buckets rather than the whole store.

## Explicit Non-Goals

Do not:

- set `GLASSLAB_WORKFLOW_API_STORE_JSON_PATH` to an `s3://` URI
- use MinIO as the workflow/session metadata store
- teach every service to pretend S3 is a local filesystem
- instantiate clients with hidden default `Settings()` inside storage helpers
- commit root object-store credentials into code or ConfigMaps
- require every workload container to know MinIO credentials in v0

## Implementation Phases

### Phase 0: Current Safe State

- Postgres is the workflow/session/run record store.
- Shared artifacts PVC is the run-artifact staging plane.
- Source documents default to filesystem mode unless MinIO credentials are wired.
- MinIO exists as a durable object store but is not yet the canonical artifact
  plane for all new runs.

### Phase 1: Source Documents To MinIO

Wire `workflow-api` with a bucket-scoped MinIO identity, not the root identity.
The scoped user lives in its own Secret (`glasslab-v2-workflow-api-minio`) and
is created by the one-shot provisioning Job; see
[Provision MinIO scoped users](runbooks/provision-minio-scoped-users.md).

Set:

```text
GLASSLAB_WORKFLOW_API_SOURCE_DOCUMENT_STORAGE_MODE=minio
GLASSLAB_WORKFLOW_API_SOURCE_DOCUMENT_BUCKET=glasslab-source-documents
GLASSLAB_WORKFLOW_API_MINIO_ENDPOINT=glasslab-minio.glasslab-v2.svc.cluster.local:9000
```

Add env wiring for:

```text
GLASSLAB_WORKFLOW_API_MINIO_ACCESS_KEY
GLASSLAB_WORKFLOW_API_MINIO_SECRET_KEY
```

The scoped policy grants the source-document bucket only; the root credential is
never mounted into workflow-api or the GPU runner. Validate source intake before
changing run artifacts.

### Phase 2: Artifact Ingest

Add a `workflow-api` artifact ingest helper:

```text
POST /experiments/runs/{run_id}/artifacts/ingest
```

The helper reads `/mnt/artifacts/{run_id}/`, validates required files, and
updates the run record's `artifact_refs` with shared-artifact paths.

This is preferred over teaching `run_artifacts.py` to read every possible URI.

### Phase 3: Comparison Promotion

Have evaluator/reporter output comparison files to:

```text
/mnt/artifacts/comparisons/{comparison_id}/
```

The `ComparisonRecord` in Postgres stores the refs.

### Phase 4: Optional Direct Writes

Only after promotion is reliable, allow workload containers to write directly to
MinIO using narrowly scoped credentials.

This is optional. The platform should work with staging plus promotion first.

## Migration Rules

- Existing `/mnt/artifacts/{run_id}` bundles remain valid.
- New records should prefer shared-artifact refs after ingest.
- Reads should prefer Postgres refs first, then fall back to the artifact path for
  older runs.
- JSON workflow state remains backup/import material only.

## Health Checks

Minimum checks before declaring MinIO-backed artifacts ready:

```bash
kubectl -n glasslab-v2 rollout status deploy/glasslab-minio --timeout=120s
kubectl -n glasslab-v2 get secret glasslab-v2-minio
kubectl -n glasslab-v2 exec deploy/glasslab-workflow-api -- sh -lc 'test -d /mnt/artifacts'
```

After ingest exists:

```bash
curl -fsS -X POST http://127.0.0.1:18081/experiments/runs/{run_id}/artifacts/ingest
curl -fsS http://127.0.0.1:18081/runs/{run_id}
```

The returned run should contain `s3://glasslab-run-artifacts/...` artifact refs.

## Bottom Line

The storage stack should be boring:

- Postgres for records
- MinIO for durable blobs
- shared filesystems for datasets and staging
- ephemeral storage for scratch

Anything else is compatibility or migration glue, not architecture.
