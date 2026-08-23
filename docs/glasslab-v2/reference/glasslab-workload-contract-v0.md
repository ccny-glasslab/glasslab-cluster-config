# Glasslab Workload Contract v0

**Date:** 2026-04-22  
**Version:** 0  
**Repository:** cluster-config  
**Scope:** Workflow execution, evaluation, and artifact lifecycle

---

## Executive Summary

Glasslab implements a **runner-first experiment platform** with explicit bounded agents and deterministic execution. The workload contract defines what Glasslab expects from external systems and what external systems can expect from Glasslab.

**Core Principle:** Glasslab is not a general-purpose agent chat system. It is a runner-first experiment platform focused on:

- Keep a bounded research session
- Turn that session into a reviewable experiment plan
- Launch approved runs
- Compare outcomes
- Record decisions
- Propose the next bounded mutation

---

## 1. Required Inputs

### 1.1 Command Surface

Glasslab accepts commands through a deterministic command surface:

```
whatsapp-gateway → research-ingress → research-command-router → workflow-api
```

**Primary Commands:**

| Command | Purpose | Endpoint Pattern | Owner |
|---------|---------|------------------|-------|
| `!new` | Create new session | `POST /research-sessions` | workflow-api |
| `!state` | Return session context | `GET /research-sessions/{id}/context` | workflow-api |
| `!add` | Add intake source (dataset/paper/note/baseline) | `POST /research-sessions/{id}/intake` | workflow-api |
| `!plan` | Create or refresh design draft | `POST /research-sessions/{id}/transitions/prepare-current-plan` | workflow-api |
| `!check` | Run design preflight/readiness | `GET /research-sessions/{id}/preflight/current-plan` | workflow-api |
| `!run` | Execute approved run | `POST /research-sessions/{id}/transitions/run-happy-path` | workflow-api |
| `!compare` | Compare runs/campaign results | `GET /research-sessions/{id}/autoresearch-model-comparison` | workflow-api |
| `!decide` | Persist human decision | `POST /research-sessions/{id}/decisions/current` | workflow-api |
| `!next` | Advance campaign, draft variants | `POST /research-sessions/{id}/transitions/advance-autoresearch` | workflow-api |

**Session Semantics:**
- Router dispatch prefers pinned session ID
- `latest` may remain internally available but primary flows should use explicit session IDs

### 1.2 Workflow Registry Inputs

Each run must reference an approved workflow from the registry:

```json
{
  "workflow_id": "generic-tabular-benchmark",
  "display_name": "Generic Tabular Benchmark",
  "workflow_family": "tabular-benchmark",
  "description": "Run approved baseline models against a tabular dataset",
  "required_inputs": [...],
  "allowed_models": [...],
  "runner_image": "ghcr.io/ccny-glasslab/glasslab-research-workspace-runner@sha256:<digest>",
  "runner_service_account_name": "glasslab-gpu-runner",
  "max_wallclock_minutes": 60,
  "evaluator_type": "tabular-metric-max",
  "expected_artifacts": {...},
  "resource_profile": {...},
  "approval_tier": "tier-2-approved-execution",
  "execution_status": "ready",
  "submission_backend": "kubernetes",
  "execution_blockers": [],
  "runtime_requirements": {...}
}
```

**Current Registry Execution State:**

Runnable workflows are fail-closed: the registry fixes a digest-pinned image,
container entrypoint, bounded resources, and runner service account. The older
specialized runner tags could not be resolved to published OCI digests, so
those definitions remain discoverable but are disabled until rebuilt and
digest-pinned.

For research-workspace workflows, `workspace.command` is a distinct bounded
payload inside the frozen source bundle. It is an argv list executed without a
shell by the fixed workspace runner, and the API accepts only the approved
`python3` executable form with bounded argument count and size. It does not
replace the registry-owned container entrypoint. The request wall-clock budget
must also be positive and no greater than the registry's fixed ceiling; the
same ceiling is carried into the run manifest and enforced when rendering the
Kubernetes Job deadline.

| Workflow ID | Family | Runner Image | Approval Tier | Status |
|-------------|--------|--------------|---------------|--------|
| `generic-tabular-benchmark` | tabular-benchmark | unresolved legacy tag | tier-2 | disabled |
| `gpu-experiment` | gpu-experiment | unresolved local tag | tier-2 | disabled |
| `literature-to-experiment` | literature-to-experiment | unresolved legacy tag | tier-2 | disabled |
| `metric-search-v0` | generic-experiment | unresolved local tag | tier-2 | disabled |
| `replication-lite` | replication-lite | unresolved legacy tag | tier-3 | disabled |
| `research-workspace-cpu-v1` | research-workspace | digest-pinned workspace runner | tier-2 | ready |
| `benchmark-workspace-cpu-v1` | research-workspace | digest-pinned workspace runner | tier-2 | ready |
| `benchmark-workspace-gpu-v1` | research-workspace | digest-pinned workspace runner | tier-2 | ready |
| `workspace-cpu-ml-v1` | research-workspace | digest-pinned workspace runner | tier-2 | ready |
| `workspace-gpu-ml-v1` | research-workspace | digest-pinned workspace runner | tier-2 | ready |

### 1.3 Required Workflow Inputs

**generic-tabular-benchmark:**
- `dataset_name` (dataset, required)
- `train_uri` (dataset, required)
- `test_uri` (dataset, required)
- `validation_strategy` (parameter_set, optional)
- `validation_split` (parameter_set, optional)
- `target_column` (parameter_set, required)

**gpu-experiment:**
- `dataset_uri` (dataset, required)
- `model_family` (parameter_set, required)
- `training_notes` (notes, required)
- `evaluation_target` (text, optional)
- `validation_strategy` (parameter_set, optional)
- `validation_split` (parameter_set, optional)
- `pair_strategy` (parameter_set, optional)
- `evaluation_protocol` (parameter_set, optional)
- `label_field` (parameter_set, optional)
- `image_field` (parameter_set, optional)
- `negative_sampling_strategy` (parameter_set, optional)

**literature-to-experiment:**
- `paper_id` (text, required)
- `source_notes` (notes, required)
- `dataset_uri` (dataset, required)
- `validation_strategy` (parameter_set, optional)
- `validation_split` (parameter_set, optional)

**replication-lite:**
- `paper_id` (text, required)
- `repository_url` (url, required)
- `dataset_uri` (dataset, required)
- `evaluation_target` (parameter_set, required)

### 1.4 Input Types

| Type | Description |
|------|-------------|
| `dataset` | Stable dataset identifier or URI |
| `paper_bundle` | Collection of paper references and notes |
| `artifact_bundle` | Pre-existing run outputs for re-run |
| `notes` | Free-form notes or annotations |
| `text` | Plain text input |
| `url` | URL reference |
| `parameter_set` | Configuration parameters |

---

## 2. Expected Outputs

### 2.1 Run Bundle Artifacts (Per-Run Outputs)

Every run **must** produce the following required artifacts:

| Artifact | Path | Type | Purpose |
|----------|------|------|---------|
| `run_manifest.json` | `{run_id}/run_manifest.json` | JSON | Run definition and metadata |
| `config.json` | `{run_id}/config.json` | JSON | Runner configuration |
| `metrics.json` | `{run_id}/metrics.json` | JSON | Run metrics and results |
| `artifacts_index.json` | `{run_id}/artifacts_index.json` | JSON | Artifact manifest |
| `report.md` | `{run_id}/report.md` | Markdown | Human-readable summary |
| `status.json` | `{run_id}/status.json` | JSON | Run status (queued/running/succeeded/failed) |
| `logs/` | `{run_id}/logs/` | Directory | Runner logs |

**Optional Artifacts by Workflow:**

| Workflow | Optional Artifacts |
|----------|-------------------|
| `generic-tabular-benchmark` | `submission.csv`, `feature_importance.csv`, `analysis_notebook.ipynb` |
| `gpu-experiment` | `checkpoint_manifest.json`, `model_card.md`, `analysis_notebook.ipynb` |
| `literature-to-experiment` | `method_spec.json`, `design_notes.md`, `analysis_notebook.ipynb` |
| `replication-lite` | `replication_delta.json`, `environment_snapshot.txt` |

### 2.2 Evaluation Output Artifacts

| Artifact | Path | Type | Purpose |
|----------|------|------|---------|
| `comparison.json` | `{evaluator_output}/comparison.json` | JSON | Multi-run comparison results |
| `summary.md` | `{evaluator_output}/summary.md` | Markdown | Human-readable comparison summary |

### 2.3 State and Metadata Artifacts (Non-Bundle)

| Artifact | Path | Purpose | Owner |
|----------|------|---------|-------|
| `state/run-store.json` | `/mnt/artifacts/workflow-api/state/run-store.json` | Workflow API session/state | workflow-api |
| `source-documents/` | `/mnt/artifacts/source-documents/` | Source paper storage | workflow-api |

### 2.4 Artifact Path Conventions

| Purpose | Path | Notes |
|---------|------|-------|
| Run output directory | `/mnt/artifacts/{run_id}/` | Shared PVC |
| Source documents | `/mnt/artifacts/source-documents/{document_id}/` | Shared PVC |
| Workflow API state | `/mnt/artifacts/workflow-api/state/run-store.json` | JSON store in shared PVC |
| Evaluator output | `/mnt/artifacts/evaluator/{comparison_id}/` | Per-comparison |

---

## 3. Resource Profile

### 3.1 CPU Profiles

**cpu-small (generic-tabular-benchmark):**
```json
{
  "requests": {
    "cpu": "500m",
    "memory": "1Gi"
  },
  "limits": {
    "cpu": "1",
    "memory": "2Gi"
  },
  "node_selector": {}
}
```

**cpu-medium (literature-to-experiment, replication-lite):**
```json
{
  "requests": {
    "cpu": "1",
    "memory": "2Gi"
  },
  "limits": {
    "cpu": "2",
    "memory": "4Gi"
  },
  "node_selector": {}
}
```

### 3.2 GPU Profiles

**gpu-small (gpu-experiment):**
```json
{
  "requests": {
    "cpu": "2",
    "memory": "4Gi",
    "nvidia.com/gpu": "1"
  },
  "limits": {
    "cpu": "4",
    "memory": "8Gi",
    "nvidia.com/gpu": "1"
  },
  "node_selector": {
    "glasslab.io/gpu-candidate": "true",
    "glasslab.io/gpu-vendor": "nvidia"
  }
}
```

### 3.3 Resource Requirements by Workflow

| Workflow | CPU | Memory | GPU | Node Selector |
|----------|-----|--------|-----|---------------|
| generic-tabular-benchmark | 500m-1 | 1Gi-2Gi | No | None |
| gpu-experiment | 2-4 | 4Gi-8Gi | 1 | GPU-candidate nodes |
| literature-to-experiment | 1-2 | 2Gi-4Gi | No | None |
| replication-lite | 1-2 | 2Gi-4Gi | No | None |

---

## 4. Evaluator Type

Glasslab supports multiple evaluator types for comparing runs:

| Evaluator Type | Workflow Families | Purpose |
|----------------|-------------------|---------|
| `tabular-metric-max` | tabular-benchmark | Maximize tabular metrics (accuracy, F1, etc.) |
| `gpu-training-metrics` | gpu-experiment | Compare GPU training runs with loss/metric tracking |
| `spec-comparison` | literature-to-experiment | Compare specified method against baseline |
| `replication-delta` | replication-lite | Measure replication fidelity delta |

### Evaluator Contract

**Input:**
- One or more completed run bundle directories
- Required artifacts: `run_manifest.json`, `metrics.json`, `status.json`

**Output:**
- `comparison.json`: Deterministic comparison results with ranking
- `summary.md`: Human-readable comparison narrative

### Comparison Logic

Evaluators produce deterministic rankings based on:

1. **Status:** Succeeded runs preferred
2. **Primary Metric:** Optimal value (direction: maximize/minimize)
3. **Runtime:** Lower runtime preferred (all else equal)
4. **Run ID:** Lexicographic tiebreaker

---

## 5. Artifact References

### 5.1 Run Manifest Schema

```python
class RunManifest(BaseModel):
    run_id: str
    workflow_id: str
    workflow_family: str
    display_name: str
    objective: str
    submitted_by: str
    submitted_at: datetime
    run_priority: Literal['user', 'autonomous']
    inputs: dict[str, Any]
    requested_models: list[str]
    resource_profile: str
    resource_requests: dict[str, str]
    resource_limits: dict[str, str]
    node_selector: dict[str, str]
    runner_image: str
    evaluator_type: str
    approval_tier: str
    expected_artifacts: dict[str, list[str]]
```

### 5.2 Metrics Schema

```python
class MetricRecord(BaseModel):
    name: str
    value: float
    direction: Literal['maximize', 'minimize']
    split: str | None = None

class Metrics(BaseModel):
    run_id: str
    primary_metric: str | None = None
    values: list[MetricRecord]
    runtime_seconds: float | None = None
    notes: list[str]
```

### 5.3 Status Schema

```python
class RunStatus(BaseModel):
    run_id: str
    status: Literal['accepted', 'queued', 'running', 'succeeded', 'failed', 'rejected']
    updated_at: datetime
    detail: str | None = None
```

### 5.4 Comparison Result Schema

```python
class ComparedRun(BaseModel):
    run_id: str
    workflow_id: str
    workflow_family: str
    models: list[str]
    status: str
    primary_metric_name: str | None = None
    primary_metric_value: float | None = None
    primary_metric_direction: str | None = None
    runtime_seconds: float | None = None

class ComparisonResult(BaseModel):
    compared_runs: list[ComparedRun]
    ranking: list[RankedRun]
    best_run_id: str | None = None
    comparison_basis: str
```

---

## 6. Failure Modes

### 6.1 Input Validation Failures

**Failure:** Invalid or missing required inputs

**Behavior:**
- Fail-closed before run submission
- No silent repair or opportunistic补全
- Return clear error indicating missing/invalid fields

**Examples:**
- Missing required dataset URI
- Invalid model name not in allowed list
- Resource profile not in approved list
- Missing approval tier authorization

### 6.2 Execution Failures

**Failure:** Run fails during execution

**Behavior:**
- Status transitions: `accepted` → `queued` → `running` → `failed`
- Log capture continues until end
- Artifact bundle still produced with status and logs
- No automatic retry without explicit schedule record

### 6.3 Artifact Completion Failures

**Failure:** Run completes but artifacts are incomplete

**Behavior:**
- Evaluation fails with explicit error about missing artifacts
- No free-form model repair
- Comparison basis explicitly states what's missing
- Run marked as `failed` or `incomplete`

### 6.4 Backend Failures

**Failure:** Backend service unavailable

**Behavior:**
- Workflow API: JSON store only
- Deterministic commands: 400-level errors with clear explanation
- No "API unreachable" language when backend returns validation error

### 6.5 Approval Tier Failures

| Tier | Description | Behavior on Violation |
|------|-------------|----------------------|
| `tier-1-read-only` | Read-only operations | Reject write attempts |
| `tier-2-approved-execution` | Pre-approved runs | Reject unapproved runs |
| `tier-3-human-approval` | Human-in-the-loop | Require explicit human decision |

### 6.6 Session State Failures

**Failure:** Session state corruption or divergence

**Behavior:**
- State stored in Postgres (not ephemeral memory)
- JSON backup on shared artifacts PVC
- Rollback procedure available in runbooks
- Stateful object recovery documented in `backup-restore-local-pv-services.md`

---

## 7. Execution Boundary

### 7.1 What Execution Owns

- Submitting run manifest to configured job-submission backend
- Creating initial accepted run record
- Resolving live status as Job progresses
- Surfacing logs and artifacts
- Supporting bounded approved-rerun schedule records

### 7.2 What Execution Should Not Own

- Reinterpreting the research goal
- Mutating the approved workflow family
- Inventing missing inputs
- Widening model or resource scope
- Bypassing registry validation
- Deciding on its own to rerun failed or drifted work

### 7.3 Current Implementation

**Services:**
- `workflow-api` - Main orchestration backend
- `runner` - Job submission and execution
- `evaluator` - Deterministic comparison
- `reporter` - Artifact-to-document transformation

**Stage Agents:**
- `intake-agent` - Normalizes raw intake requests
- `interpretation-agent` - Produces bounded interpretation drafts
- `assessment-agent` - Produces replicability assessments
- `design-agent` - Produces bounded design drafts

---

## 8. Deployment Requirements

### 8.1 Infrastructure Prerequisites

**PVCs Required:**
- `glasslab-shared-datasets` - Shared datasets (NFS RWX)
- `glasslab-shared-artifacts` - Run artifacts (NFS RWX)

**Stateful Services:**
- `glasslab-postgres` - Session and workflow state (local PV)
- `glasslab-minio` - Object storage (local PV)
- `glasslab-nats` - Message queue (local PV)

**Secrets Required:**
- `glasslab-v2-postgres` - Database credentials
- `glasslab-v2-minio` - Object storage credentials
- `glasslab-v2-workflow-api` - Workflow API configuration
- `glasslab-whatsapp-gateway` - WhatsApp integration
- `glasslab-ghcr-pull` - Docker registry credentials

### 8.2 Image Requirements

| Service | Image Pattern | Approval |
|---------|---------------|----------|
| workflow-api | ghcr.io/ccny-glasslab/glasslab-workflow-api | GHCR |
| intake-agent | ghcr.io/ccny-glasslab/glasslab-intake-agent | GHCR |
| interpretation-agent | ghcr.io/ccny-glasslab/glasslab-interpretation-agent | GHCR |
| assessment-agent | ghcr.io/ccny-glasslab/glasslab-assessment-agent | GHCR |
| design-agent | ghcr.io/ccny-glasslab/glasslab-design-agent | GHCR |

### 8.3 Environment Variables

**workflow-api:**
- `GLASSLAB_WORKFLOW_API_STORE_BACKEND=postgres|json|memory`
- `GLASSLAB_WORKFLOW_API_STORE_POSTGRES_DSN`
- `GLASSLAB_WORKFLOW_API_STORE_JSON_PATH`
- `GLASSLAB_WORKFLOW_API_SOURCE_DOCUMENT_STORAGE_MODE=filesystem|minio`
- `GLASSLAB_WORKFLOW_API_INTAKE_AGENT_ENABLED`
- `GLASSLAB_WORKFLOW_API_INTAKE_AGENT_URL`
- `GLASSLAB_WORKFLOW_API_INTAKE_AGENT_TIMEOUT_SECONDS`
- `GLASSLAB_WORKFLOW_API_INTERPRETATION_AGENT_ENABLED`
- `GLASSLAB_WORKFLOW_API_INTERPRETATION_AGENT_URL`
- `GLASSLAB_WORKFLOW_API_INTERPRETATION_AGENT_TIMEOUT_SECONDS`

---

## 9. Operational Runbooks

**Deploy:**
- `runbooks/deploy-v2.md` - Full deployment procedure
- `runbooks/predeploy-v2.md` - Pre-deployment checks
- `runbooks/deploy-bounded-agents.md` - Bounded agent deployment

**Rollback:**
- `runbooks/rollback-v2.md` - Rollback procedure

**Backup and Restore:**
- `runbooks/backup-restore-local-pv-services.md` - Stateful service backup
- `runbooks/restore-v2-secrets.md` - Secret restoration

**Workflow Management:**
- `runbooks/add-workflow-family.md` - Add new workflow family
- `runbooks/validate-chat-channel.md` - Command channel validation

---

## 10. Current Production Posture

**As of 2026-04-22:**

- **Canonical repo:** `.44` (`192.168.1.44`)
- **Active namespace:** `glasslab-v2`
- **Store backend:** Postgres
- **Artifact storage:** Shared NFS PVC (`/mnt/artifacts`)
- **Source document storage:** Shared NFS PVC (`/mnt/artifacts/source-documents`)
- **Model serving:** exo (mlx-community/Qwen3-Coder-Next-4bit on .21/.19)
- **Command surface:** whatsapp-gateway → research-ingress → research-command-router → workflow-api

---

## 11. References

- `docs/glasslab-v2/canonical-stack-2026-04.md` - Canonical stack definition
- `docs/glasslab-v2/router-and-backend-contract.md` - Command routing contract
- `docs/glasslab-v2/artifact-contract-audit-2026-04.md` - Artifact contract audit
- `docs/glasslab-v2/bounded-agent-architecture.md` - Bounded agent architecture
- `docs/glasslab-v2/execution-boundary.md` - Execution boundary definition
- `docs/glasslab-v2/evaluation-boundary.md` - Evaluation boundary definition
- `docs/glasslab-v2/autonomous-research-lane.md` - Unattended research lane
- `docs/glasslab-v2/state-and-storage.md` - Storage and state strategy
- `docs/glasslab-v2/storage-and-state.md` - Storage and state map
- `docs/glasslab-v2/stateful-object-inventory-2026-04.md` - Stateful object inventory
- `services/common/schemas/run_artifacts.py` - Artifact schemas
- `services/common/schemas/workflow_registry.py` - Workflow registry schemas
- `services/workflow-registry/definitions/` - Workflow definition files

---

## 12. Version History

| Version | Date | Changes |
|---------|------|---------|
| 0 | 2026-04-22 | Initial draft from cluster-config audit |
