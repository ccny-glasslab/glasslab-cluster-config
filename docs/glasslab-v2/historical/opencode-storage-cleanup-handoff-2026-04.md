# OpenCode Storage Cleanup Handoff 2026-04

This note summarizes the cleanup after the OpenCode changes that landed after
the first generic `metric-search-v0` run path worked.

It is written for the next OpenCode/Codex pass so it does not repeat the same
mistakes.

## Current Baseline

The safe repo baseline is:

```text
2fadf37 Clean up OpenCode storage changes
```

At this point:

- `metric-search-v0` is registered as a generic experiment workload.
- `workflow-api` has generic experiment submit/result-ingest/compare endpoints.
- a real `metric-search-v0` run has already completed live through Kubernetes.
- Postgres is the live record store.
- shared artifacts PVC is the current run-artifact staging plane.
- MinIO exists and should become the durable blob plane, but is not fully wired
  as the canonical artifact path yet.

Read this before editing storage code:

- `storage-contract-2026-04.md`
- `artifact-contract.md`
- `stateful-object-inventory-2026-04.md`
- `generic-experiment-implementation-plan.md`

## What Was Good In The OpenCode Pass

OpenCode moved in the right conceptual direction on three points:

1. It identified that artifact refs should move toward durable object refs
   instead of raw local paths.
2. It noticed that dataset references need a resolution layer.
3. It started an `art-retrieval-v1` evaluator, which is directionally useful
   for `glasslab-metric-search`.

Those ideas were kept or preserved where safe.

Useful additions kept:

- `docs/glasslab-v2/artifact-contract-audit-2026-04.md`
- `docs/glasslab-v2/doc-contradictions-2026-04.md`
- `docs/glasslab-v2/generic-experiment-gap-audit-2026-04.md`
- `docs/glasslab-v2/workload-registry-evolution-notes-2026-04.md`
- `docs/glasslab-v2/reference/glasslab-workload-contract-v0.md`
- `services/evaluator/app/art_retrieval_v1.py`

## Where OpenCode Went Wrong

### 1. It committed a secret-bearing script

`submit_titanic.sh` contained a Kaggle API token.

That file was removed from HEAD, but the token was already pushed in Git
history. If the token was real, revoke it. Do not solve this by adding another
cleanup commit with the token removed; that only removes it from HEAD.

Rule:

- never commit user API tokens
- never add one-off submission scripts with embedded credentials
- use Kubernetes Secrets, `.44` local ignored manifests, or environment
  variables only

### 2. It reintroduced old Titanic/v1 artifacts at repo root

The root-level Titanic scripts and markdown outputs were deleted.

Removed:

- `AUDIT_SUMMARY.md`
- `TITANIC_OUTPUT.md`
- `TITANIC_TEST.md`
- `run_titanic.py`
- `run_titanic_kaggle.py`
- `scripts/overfitting_analysis.py`
- `submit_titanic.sh`

Why:

- they were not part of the current runner-first product shape
- they mixed old Titanic/v1 assumptions into the generic experiment work
- they created floating scripts with unclear ownership

If Titanic examples are needed again, put them under a deliberate reference or
example path, without secrets, and make clear they are not the primary product.

### 3. It treated MinIO like a drop-in filesystem replacement

This was the biggest architectural mistake.

Do not make helpers accept both `Path` and `s3://...` strings by scattering
URI checks through file readers. That creates a fake filesystem abstraction and
makes every service partially responsible for storage semantics.

Specifically, do not:

- set `GLASSLAB_WORKFLOW_API_STORE_JSON_PATH` to an `s3://...` URI
- teach `run_artifacts.py` to pretend S3 paths are local directories
- instantiate `Settings()` inside storage helpers instead of using the app's
  provided settings
- create MinIO clients with missing credentials
- switch defaults to MinIO before manifests actually provide credentials

The cleanup reverted those changes.

### 4. It changed runtime defaults before wiring credentials

OpenCode changed source-document storage default to MinIO, but the deployment
did not provide:

- `GLASSLAB_WORKFLOW_API_MINIO_ACCESS_KEY`
- `GLASSLAB_WORKFLOW_API_MINIO_SECRET_KEY`

That would make source intake fail at runtime.

Rule:

- do not flip a default to a new backend until the deployment, secret wiring,
  health check, and smoke path are all present

### 5. It broke reporter tests

The reporter change removed required imports and made
`services/reporter/tests/test_main.py` fail.

The cleanup restored the simple filesystem-only reporter path.

Rule:

- run the narrow test for every touched service
- if a migration requires new storage behavior, add tests for that behavior
  before changing defaults

## The Correct Storage Direction

The direction is not "everything becomes MinIO."

The direction is:

```text
Postgres -> records
MinIO -> durable blobs/artifacts
shared filesystem -> datasets and staging
pod-local filesystem -> scratch
Kubernetes Secrets -> runtime secrets
```

Do not make stores interchangeable.

### Records

Postgres owns:

- sessions
- source metadata
- designs
- runs
- decisions
- comparisons
- lineage

### Blobs and artifacts

MinIO should own:

- source-document blobs
- completed run artifact bundles
- comparison reports and evaluator outputs

### Staging

The shared artifacts PVC still matters.

For now, Kubernetes Jobs should write to:

```text
/mnt/artifacts/{run_id}/
```

Then `workflow-api` should promote the completed bundle to MinIO and store the
resulting object refs in Postgres.

This avoids forcing every workload image to carry MinIO credentials in v0.

## What To Do Next

Implement storage in phases.

### Phase 1: Wire MinIO credentials to `workflow-api`

Add env wiring to the workflow-api deployment:

```text
GLASSLAB_WORKFLOW_API_MINIO_ACCESS_KEY
GLASSLAB_WORKFLOW_API_MINIO_SECRET_KEY
GLASSLAB_WORKFLOW_API_MINIO_ENDPOINT
```

Use the existing `glasslab-v2-minio` Secret as the source.

Do not change artifact storage behavior yet.

Validation:

- `workflow-api /healthz` should not expose secrets
- source-document MinIO mode should be testable without affecting run artifacts

### Phase 2: Move source-document blobs to MinIO

This is the smallest safe MinIO cutover.

Set:

```text
GLASSLAB_WORKFLOW_API_SOURCE_DOCUMENT_STORAGE_MODE=minio
GLASSLAB_WORKFLOW_API_SOURCE_DOCUMENT_BUCKET=glasslab-source-documents
```

Then validate one source intake path.

If this fails, revert only source-document mode. Do not touch run artifacts.

### Phase 3: Add backend-owned artifact promotion

Add a new backend-owned promotion path.

Target shape:

```text
POST /experiments/runs/{run_id}/artifacts/promote
```

Behavior:

1. read `/mnt/artifacts/{run_id}/`
2. verify required files exist
3. upload to `s3://glasslab-run-artifacts/runs/{run_id}/...`
4. update the run record's `artifact_refs`
5. be idempotent

This is the key missing implementation step.

### Phase 4: Promote comparison artifacts

Comparison records already exist in Postgres.

Next, comparison reports should be promoted to:

```text
s3://glasslab-comparisons/comparisons/{comparison_id}/...
```

Then store the refs in `ComparisonRecord.artifact_refs`.

### Phase 5: Optional direct-to-MinIO workload writes

Only after promotion works should workload containers optionally write directly
to MinIO.

Even then, use narrowly scoped credentials. Do not hand root MinIO credentials
to arbitrary workload containers.

## What Not To Do Next

Do not:

- roll `origin/main` live without tests
- resurrect root-level Titanic scripts
- put Kaggle or other API tokens in the repo
- change JSON store paths to `s3://...`
- make `run_artifacts.py` a fake universal filesystem layer
- convert all services to MinIO at once
- make the runner image responsible for platform storage semantics

## Tests To Run Before The Next Rollout

At minimum:

```bash
cd services/reporter
pytest tests/test_main.py -q

cd ../evaluator
pytest -q

cd ../workflow-api
pytest tests/test_persistence.py -q
pytest tests/test_api.py -q -k 'healthz_and_workflow_families or generic_experiment_run_result_ingest_and_compare'
```

Then from `.44`:

```bash
cd /home/glasslab/cluster-config
./scripts/smoke-test-v2.sh
```

If changing images, build, push, roll, and validate the exact deployed SHA.

## Bottom Line

The MinIO instinct was correct. The implementation was too broad and too
implicit.

The next good implementation is not "make every path accept S3."

The next good implementation is:

1. wire credentials safely
2. cut over source-document blobs first
3. add explicit artifact promotion from PVC staging to MinIO
4. store object refs in Postgres
5. keep workloads simple until the platform path is proven
