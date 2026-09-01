# Workflow Registry Schema Audit & Workload Evolution Notes

**Date:** 2026-04-22  
**Repository:** `/Users/glasslab/cluster-config`  
**Scope:** `services/workflow-registry`  
**Author:** Audit analysis for generic workload support

---

## Executive Summary

The workflow registry currently defines **four workflow families**, all specialized for specific experiment types (GPU training, literature-to-experiment, replication, tabular benchmarking). The schema supports generic experiment execution in the underlying system (`workflow-api`, `evaluator`, `reporter`) but the registry itself is heavily coupled to legacy "workflow-family" thinking.

**Key Finding:** The registry schema contains enough flexibility for generic workloads, but three of four existing definitions embed workflow-family constraints that are too specific. A minimal schema refactor is needed to decouple generic workload execution from legacy experiment taxonomy.

**No code changes required.** This document documents current state and recommended field-level migrations.

---

## Current Schema Fields

### Schema Definition (`services/common/schemas/workflow_registry.py`)

The registry entry schema consists of the following fields (all required unless noted):

| Field | Type | Current Values | Description |
|-------|------|----------------|-------------|
| `workflow_id` | `str` | `gpu-experiment`, `literature-to-experiment`, `replication-lite`, `generic-tabular-benchmark` | Stable machine-readable identifier |
| `display_name` | `str` | Human-readable names | Operator-facing label |
| `workflow_family` | `str` | Matches `workflow_id` | Coarse execution-template identifier |
| `description` | `str` | Detailed descriptions | Narrow statement of allowed operations |
| `required_inputs` | `list[WorkflowInputSpec]` | Variable per workflow | Input specifications with `name`, `input_type`, `required`, `description` |
| `allowed_models` | `list[str]` | Variable per workflow | Model identifiers the workflow may request |
| `runner_image` | `str` | Container image URIs | Approved executor container |
| `evaluator_type` | `str` | `gpu-training-metrics`, `spec-comparison`, `replication-delta`, `tabular-metric-max` | Deterministic evaluator category |
| `expected_artifacts` | `ExpectedArtifactsSpec` | Structured artifact list | Required and optional artifact names |
| `resource_profile` | `ResourceProfileSpec` | CPU/GPU profiles | Resource requests/limits/node_selector |
| `approval_tier` | `ApprovalTier` | `tier-1-read-only`, `tier-2-approved-execution`, `tier-3-human-approval` | Policy tier for execution |
| `execution_status` | `ExecutionStatus` | `ready`, `experimental`, `declared_only`, `disabled` | Execution readiness flag |
| `submission_backend` | `SubmissionBackend` | `kubernetes`, `null`, `unimplemented` | Backend for job submission |
| `execution_blockers` | `list[str]` | Variable per workflow | Known blockers preventing execution |
| `runtime_requirements` | `dict` | Variable per workflow | Runtime constraints (GPU, Python packages, modalities, etc.) |

---

## Current Registry Entries Analysis

### 1. `gpu-experiment`

| Field | Assessment | Generic Workload Compatible? |
|-------|-----------|------------------------------|
| `workflow_id` | Generic ✅ | Yes |
| `workflow_family` | Too specific ❌ | No—hardcoded to "gpu-experiment" |
| `description` | Moderately generic ✅ | Supports computer vision, tabular, ML |
| `required_inputs` | Mixed ⚠️ | `dataset_uri`, `model_family`, `training_notes` are generic; `evaluation_target`, `validation_strategy` are generic; `label_field`, `image_field`, `negative_sampling_strategy` are too specific to vision experiments |
| `allowed_models` | Generic ✅ | `pytorch-template-v1`, `lightning-template-v1`, `deterministic-template` |
| `runner_image` | Generic ✅ | GPU runner |
| `evaluator_type` | Too specific ❌ | `gpu-training-metrics` implies training, not evaluation/inference |
| `expected_artifacts` | Generic ✅ | Standard run bundle artifacts |
| `resource_profile` | Generic ✅ | GPU-candidate node selector |
| `approval_tier` | Generic ✅ | Tier-2 is appropriate |
| `execution_status` | Generic ✅ | Ready |
| `submission_backend` | Generic ✅ | Kubernetes |
| `execution_blockers` | Generic ✅ | Empty list |
| `runtime_requirements` | Too specific ❌ | Hardcoded to `pytorch`, `torchvision`, `timm`, `computer_vision` modalities |

**Gap:** The `runtime_requirements.modalities` field locks this to vision tasks; a generic GPU workload entry should not require `computer_vision`.

**Recommendation:** Split into two entries:
- `gpu-experiment` → GPU training (keep current, rename to `gpu-training`)
- `gpu-inference` → GPU evaluation/inference (new, generic, no modality constraints)

---

### 2. `literature-to-experiment`

| Field | Assessment | Generic Workload Compatible? |
|-------|-----------|------------------------------|
| `workflow_id` | Too specific ❌ | Implies literature-only source |
| `workflow_family` | Too specific ❌ | "literature-to-experiment" is a use case, not an execution shape |
| `description` | Too specific ❌ | "Translate reviewed paper notes into experiment" |
| `required_inputs` | Too specific ❌ | `paper_id`, `source_notes` are literature-specific |
| `allowed_models` | Too specific ❌ | `qwen3-4b-instruct-2507`, `deterministic-template` |
| `runner_image` | Generic ✅ | Literature runner |
| `evaluator_type` | Generic ✅ | `spec-comparison` |
| `expected_artifacts` | Generic ✅ | Standard run bundle artifacts |
| `resource_profile` | Generic ✅ | CPU profile |
| `approval_tier` | Generic ✅ | Tier-2 |
| `execution_status` | Generic ✅ | Ready |
| `submission_backend` | Generic ✅ | Kubernetes |
| `execution_blockers` | Generic ✅ | Empty |
| `runtime_requirements` | Not present | — |

**Gap:** This entry conflates "how to generate the experiment" (literature review) with "what to execute" (the experiment itself). A generic workflow should accept *any* experiment specification source.

**Recommendation:** Deprecate this entry. Replace with generic `experiment-run` workflow that accepts `experiment_spec_uri` instead of `paper_id`/`source_notes`.

---

### 3. `replication-lite`

| Field | Assessment | Generic Workload Compatible? |
|-------|-----------|------------------------------|
| `workflow_id` | Too specific ❌ | "replication" implies a specific use case |
| `workflow_family` | Too specific ❌ | Same as workflow_id |
| `description` | Too specific ❌ | "Re-run a narrow, approved subset of a paper or repository workflow" |
| `required_inputs` | Too specific ❌ | `repository_url`, `evaluation_target` (replication-specific) |
| `allowed_models` | Too specific ❌ | `replication-template-v1`, `qwen3-4b-instruct-2507` |
| `runner_image` | Generic ✅ | Replication runner |
| `evaluator_type` | Too specific ❌ | `replication-delta` |
| `expected_artifacts` | Generic ✅ | Standard plus replication-specific |
| `resource_profile` | Generic ✅ | CPU profile |
| `approval_tier` | Generic ✅ | Tier-3 (human approval) |
| `execution_status` | ❌ | `declared_only` with unimplemented backend |
| `submission_backend` | ❌ | `unimplemented` |
| `execution_blockers` | ❌ | "runner-spec generation is not implemented" |
| `runtime_requirements` | Not present | — |

**Gap:** This entry is marked `declared_only` with `unimplemented` backend—meaning it is *not executable*. It should either be completed or removed.

**Recommendation:** Either:
- Complete the implementation and rename to `experiment-replication`
- Deprecate and remove from registry

---

### 4. `generic-tabular-benchmark`

| Field | Assessment | Generic Workload Compatible? |
|-------|-----------|------------------------------|
| `workflow_id` | Generic ✅ | "generic-tabular-benchmark" describes purpose clearly |
| `workflow_family` | Too specific ❌ | "tabular-benchmark" excludes non-tabular workloads |
| `description` | Generic ✅ | "Run approved baseline models against a tabular dataset" |
| `required_inputs` | Generic ✅ | `dataset_name`, `train_uri`, `test_uri`, `validation_strategy`, `target_column` are generic |
| `allowed_models` | Generic ✅ | `logistic_regression`, `random_forest`, `xgboost_optional` |
| `runner_image` | Generic ✅ | Tabular runner |
| `evaluator_type` | Generic ✅ | `tabular-metric-max` |
| `expected_artifacts` | Generic ✅ | Standard run bundle artifacts |
| `resource_profile` | Generic ✅ | CPU-small profile |
| `approval_tier` | Generic ✅ | Tier-2 |
| `execution_status` | Generic ✅ | Ready |
| `submission_backend` | Generic ✅ | Kubernetes |
| `execution_blockers` | Generic ✅ | Empty |
| `runtime_requirements` | Not present | — |

**Gap:** The only truly generic entry, but `workflow_family` and `display_name` still tie it to tabular domain.

**Recommendation:** Rename to `baseline-model-benchmark` and update `runtime_requirements` to include `modalities: ["tabular"]` or remove modality constraints entirely for truly generic use.

---

## Field-by-Field Compatibility Matrix

| Field | Generic Support | Current Over-Specialization | Recommendation |
|-------|----------------|----------------------------|----------------|
| `workflow_id` | ✅ Good | Uses domain-specific names (`literature-to-experiment`, `replication-lite`) | Use neutral names: `experiment-run`, `gpu-inference`, `baseline-benchmark` |
| `workflow_family` | ❌ Poor | Same as `workflow_id`, domain-specific | Deprecate this field entirely or make it optional metadata |
| `description` | ✅ Good | Some are too narrow (literature-only, replication-only) | Standardize to: "Execute a bounded experiment with specified inputs, models, and resource constraints" |
| `required_inputs` | ⚠️ Mixed | Vision-specific (`image_field`, `negative_sampling_strategy`), literature-specific (`paper_id`, `source_notes`), replication-specific (`repository_url`) | Replace domain-specific inputs with generic equivalents: `dataset_uri`, `experiment_spec`, `model_spec` |
| `allowed_models` | ⚠️ Mixed | Some entries hard-code specific models (`qwen3-4b-instruct-2507`) | Use templates (`pytorch-template-v1`, `deterministic-template`) instead of concrete model IDs |
| `runner_image` | ✅ Good | Generic per-domain runners | Keep as-is; runners can be domain-specialized |
| `evaluator_type` | ❌ Poor | Domain-specific evaluators (`gpu-training-metrics`, `replication-delta`) | Standardize evaluators: `run-metrics`, `comparison`, `validation` |
| `expected_artifacts` | ✅ Good | Standardized run bundle | Keep as-is; already generic |
| `resource_profile` | ✅ Good | Generic CPU/GPU profiles | Keep as-is; already generic |
| `approval_tier` | ✅ Good | Generic tiers | Keep as-is; already generic |
| `execution_status` | ✅ Good | Generic status enum | Keep as-is; already generic |
| `submission_backend` | ✅ Good | Generic backends | Keep as-is; already generic |
| `execution_blockers` | ✅ Good | Generic blockers list | Keep as-is; already generic |
| `runtime_requirements` | ❌ Poor | Hardcoded modalities and packages | Make entirely optional; move domain constraints to `allowed_models` or metadata |

---

## Minimum Required Additions for Generic Workloads

### 1. `experiment_type` (NEW FIELD)

**Purpose:** Decouple workload type from workflow family. Allow operators to classify experiments without creating new registry entries.

**Proposed Schema:**
```python
experiment_type: Literal[
    "training", 
    "evaluation", 
    "inference", 
    "benchmark", 
    "replication", 
    "ab_test", 
    "hyperparameter_search",
    "feature_analysis",
    "custom"
] = "custom"
```

**Default:** `custom` (generic fallback)

**Rationale:** This allows a single generic `experiment-run` workflow to handle training, evaluation, inference, etc., based on `experiment_type` metadata rather than separate workflow definitions.

---

### 2. `workload_id` (NEW FIELD)

**Purpose:** Assign a unique, stable identifier to the workload *independent* of the workflow family. Enables tracking, comparison, and reuse of workload configurations.

**Proposed Schema:**
```python
workload_id: str = Field(min_length=3, pattern="^[a-z0-9][a-z0-9-]*[a-z0-9]$")  # kebab-case UUID-like identifier
```

**Default:** Auto-generated on run submission (e.g., `wl-{timestamp}-{hash}`)

**Rationale:** Current `workflow_id` is used for workflow *family* matching, not workload *instance* identification. A separate `workload_id` enables:
- Workload versioning
- Cross-experiment tracking
- Run grouping and comparison

---

### 3. `schema_version` / `ref` (NEW FIELDS)

**Purpose:** Version the registry schema and reference specific schema revisions for backward compatibility.

**Proposed Schema:**
```python
schema_version: str = "1.0.0"  # SemVer-style version
ref: str | None = None  # Optional reference to external schema (e.g., GitHub commit hash)
```

**Default:** `"1.0.0"` (initial stable release)

**Rationale:** Enables schema evolution without breaking existing workflow definitions. The `ref` field allows linking to exact schema revisions for auditability.

---

### 4. `artifact_contract` (NEW FIELD)

**Purpose:** Explicitly declare artifact versioning and validation requirements beyond the current static `expected_artifacts` list.

**Proposed Schema:**
```python
artifact_contract: dict[str, Any] = Field(default_factory=lambda: {
    "version": "1.0.0",
    "required": [
        {"name": "run_manifest.json", "schema": "run-manifest-v1"},
        {"name": "config.json", "schema": "config-v1"},
        {"name": "metrics.json", "schema": "metrics-v1"},
        {"name": "artifacts_index.json", "schema": "artifacts-index-v1"},
        {"name": "report.md", "schema": "report-md-v1"},
        {"name": "status.json", "schema": "run-status-v1"},
        {"name": "logs/", "type": "directory"}
    ],
    "optional": [
        {"name": "analysis_notebook.ipynb", "schema": "notebook-v1"},
        {"name": "predictions.csv", "schema": "csv-v1"},
        {"name": "features.csv", "schema": "csv-v1"},
        {"name": "checkpoints.json", "schema": "checkpoints-v1"},
        {"name": "model_card.md", "schema": "markdown-v1"},
        {"name": "method_spec.json", "schema": "json-v1"},
        {"name": "design_notes.md", "schema": "markdown-v1"},
        {"name": "replication_delta.json", "schema": "json-v1"},
        {"name": "environment_snapshot.txt", "schema": "text-v1"}
    ],
    "validation": {
        "on_submit": False,
        "on_complete": True,
        "strict": False
    }
})
```

**Rationale:** The current `expected_artifacts` is a simple list. A full `artifact_contract` enables:
- Artifact versioning
- Schema validation
- Optional vs required distinction with metadata
- Validation policy (submit-time vs completion-time, strict vs lenient)

---

### 5. `metric_contract` (NEW FIELD)

**Purpose:** Declare metric semantics, validation rules, and comparison strategies for deterministic evaluation.

**Proposed Schema:**
```python
metric_contract: dict[str, Any] = Field(default_factory=lambda: {
    "version": "1.0.0",
    "required_metrics": [],
    "optional_metrics": [],
    "primary_metric": None,
    "metrics": [],
    "comparison_strategy": "best-by-primary",
    "validations": {
        "no_nan": True,
        "positive_only": False,
        "bounded": {"min": None, "max": None}
    },
    "directions": {
        "maximize": [],
        "minimize": []
    }
})
```

**Rationale:** Current `evaluator_type` is a string without validation or semantics. A `metric_contract` enables:
- Metric validation (NaN checks, bounds)
- Primary metric declaration
- Multi-metric comparison strategies
- Directionality (maximize/minimize) per metric

---

## Migration Path

### Phase 1: Schema Enhancement (No Code Changes)

1. Add new fields to schema definition:
   - `experiment_type` (optional, default: `"custom"`)
   - `workload_id` (auto-generated, optional)
   - `schema_version` (default: `"1.0.0"`)
   - `artifact_contract` (default: current `expected_artifacts` structure)
   - `metric_contract` (default: empty, evaluable via `evaluator_type` fallback)

2. Update workflow registry definitions:
   - Add `schema_version: "1.0.0"` to all existing entries
   - Add `artifact_contract` matching current `expected_artifacts`
   - Add `metric_contract` with minimal fields (can be extended later)

3. Document current `workflow_id` → `experiment_type` mapping:
   - `gpu-experiment` → `experiment_type: "training"`
   - `literature-to-experiment` → `experiment_type: "custom"` (deprecated)
   - `replication-lite` → `experiment_type: "replication"` (deprecated)
   - `generic-tabular-benchmark` → `experiment_type: "benchmark"`

### Phase 2: New Generic Entry

Create a single generic workflow definition:

```json
{
  "workflow_id": "experiment-run",
  "workflow_family": "generic-experiment",
  "display_name": "Generic Experiment Run",
  "description": "Execute any bounded experiment with configurable inputs, models, and resource profiles.",
  "required_inputs": [
    {"name": "dataset_uri", "input_type": "dataset", "required": true, "description": "Resolved dataset location"},
    {"name": "model_spec", "input_type": "parameter_set", "required": true, "description": "Model configuration or identifier"},
    {"name": "experiment_spec", "input_type": "notes", "required": true, "description": "Experiment specification document"}
  ],
  "allowed_models": ["*"],
  "runner_image": "ghcr.io/ccny-glasslab/glasslab-generic-runner:1.0.0",
  "evaluator_type": "generic-metric-comparison",
  "expected_artifacts": {
    "required": ["run_manifest.json", "config.json", "metrics.json", "artifacts_index.json", "report.md", "status.json", "logs/"],
    "optional": ["analysis_notebook.ipynb", "predictions.csv", "features.csv"]
  },
  "resource_profile": {
    "profile_name": "auto",
    "requests": {},
    "limits": {},
    "node_selector": {}
  },
  "approval_tier": "tier-2-approved-execution",
  "execution_status": "ready",
  "submission_backend": "kubernetes",
  "execution_blockers": [],
  "runtime_requirements": {},
  "experiment_type": "custom",
  "artifact_contract": {
    "version": "1.0.0",
    "required": ["run_manifest.json", "config.json", "metrics.json", "artifacts_index.json", "report.md", "status.json", "logs/"],
    "optional": ["analysis_notebook.ipynb", "predictions.csv", "features.csv", "checkpoints.json", "model_card.md"],
    "validation": {"on_submit": false, "on_complete": true, "strict": false}
  },
  "metric_contract": {
    "version": "1.0.0",
    "required_metrics": [],
    "optional_metrics": [],
    "primary_metric": null,
    "comparison_strategy": "best-by-primary",
    "validations": {"no_nan": true, "positive_only": false, "bounded": {"min": null, "max": null}},
    "directions": {"maximize": [], "minimize": []}
  }
}
```

This single entry can handle:
- Training (via `experiment_type: "training"`)
- Evaluation (via `experiment_type: "evaluation"`)
- Inference (via `experiment_type: "inference"`)
- Replication (via `experiment_type: "replication"`)
- Benchmarking (via `experiment_type: "benchmark"`)

### Phase 3: Deprecation

1. Mark legacy entries as deprecated:
   - `literature-to-experiment`: `execution_status: "disabled"`, add `deprecation_notice`
   - `replication-lite`: `execution_status: "disabled"`, add `deprecation_notice`

2. Update documentation:
   - Redirect users to `experiment-run` with appropriate `experiment_type`
   - Document migration examples for each legacy use case

---

## Migration Notes for Existing Workflow Families

### `gpu-experiment` → `experiment-run`

**Before:**
```json
{
  "workflow_id": "gpu-experiment",
  "workflow_family": "gpu-experiment",
  "description": "Run a bounded GPU-backed experiment on a GPU worker...",
  "required_inputs": [
    {"name": "dataset_uri", "input_type": "dataset", ...},
    {"name": "model_family", "input_type": "parameter_set", ...},
    {"name": "training_notes", "input_type": "notes", ...},
    {"name": "image_field", "input_type": "parameter_set", ...},  // Vision-specific
    {"name": "negative_sampling_strategy", "input_type": "parameter_set", ...}  // Vision-specific
  ],
  "runtime_requirements": {
    "gpu": true,
    "training_stack": ["pytorch", "torchvision", "timm"],
    "modalities": ["computer_vision", "tabular"],
    ...
  }
}
```

**After:**
```json
{
  "workflow_id": "experiment-run",
  "workflow_family": "generic-experiment",
  "experiment_type": "training",
  "description": "Execute a bounded GPU-backed experiment...",
  "required_inputs": [
    {"name": "dataset_uri", "input_type": "dataset", ...},
    {"name": "model_spec", "input_type": "parameter_set", ...},
    {"name": "experiment_spec", "input_type": "notes", ...}
  ],
  "runtime_requirements": {
    "gpu": true
  },
  "artifact_contract": {
    "version": "1.0.0",
    "required": [...],
    "optional": ["checkpoint_manifest.json", "model_card.md"]  // GPU-specific optional artifacts
  }
}
```

**Migration Guide:**
- Replace `model_family` with `model_spec` (JSON object instead of string)
- Replace domain-specific inputs (`image_field`, `negative_sampling_strategy`) with `experiment_spec` (notes describing these constraints)
- Update `runtime_requirements` to remove modalities list (modalities belong in `experiment_spec`)
- Move GPU-specific optional artifacts to `artifact_contract.optional`

### `literature-to-experiment` → Deprecate

**Status:** `execution_status: "disabled"`  
**Reason:** Conflates literature review with experiment execution.

**Migration Path:**
1. Operators submit experiment specification directly (not via literature conversion)
2. Use `experiment-run` with `experiment_type: "custom"`
3. Document literature-to-experiment conversion as a *separate service* (not a workflow family)

### `replication-lite` → Deprecate or Complete

**Status:** `execution_status: "declared_only"` with unimplemented backend  
**Reason:** Not executable.

**Options:**
1. **Deprecate** (recommended): Mark as disabled, users use `experiment-run` with `experiment_type: "replication"`
2. **Complete**: Implement backend, rename to `experiment-replication`, add `replication_delta.json` to `artifact_contract.optional`

### `generic-tabular-benchmark` → `experiment-run`

**Before:**
```json
{
  "workflow_id": "generic-tabular-benchmark",
  "workflow_family": "tabular-benchmark",
  "description": "Run approved baseline models against a tabular dataset...",
  "required_inputs": [
    {"name": "dataset_name", "input_type": "dataset", ...},
    {"name": "train_uri", "input_type": "dataset", ...},
    {"name": "test_uri", "input_type": "dataset", ...},
    {"name": "target_column", "input_type": "parameter_set", ...}
  ],
  "allowed_models": ["logistic_regression", "random_forest", "xgboost_optional"]
}
```

**After:**
```json
{
  "workflow_id": "experiment-run",
  "workflow_family": "generic-experiment",
  "experiment_type": "benchmark",
  "description": "Execute a generic benchmark experiment...",
  "required_inputs": [
    {"name": "dataset_uri", "input_type": "dataset", ...},
    {"name": "model_spec", "input_type": "parameter_set", ...},
    {"name": "experiment_spec", "input_type": "notes", ...}
  ],
  "allowed_models": ["*"],
  "artifact_contract": {
    "version": "1.0.0",
    "optional": ["submission.csv", "feature_importance.csv"]
  }
}
```

**Migration Guide:**
- Replace domain-specific inputs (`dataset_name`, `train_uri`, `test_uri`, `target_column`) with generic `dataset_uri` and `experiment_spec`
- Replace `allowed_models` list with wildcard `["*"]` (model selection moves to `experiment_spec`)
- Move tabular-specific optional artifacts (`submission.csv`, `feature_importance.csv`) to `artifact_contract.optional`

---

## Field-Level Migration Summary

| Field | Status | Migration Required | Notes |
|-------|--------|-------------------|-------|
| `workflow_id` | ✅ Current | Yes | Replace domain-specific IDs with neutral `experiment-run` |
| `workflow_family` | ⚠️ Legacy | Yes/No | Either rename to generic or deprecate field entirely |
| `description` | ⚠️ Mixed | Yes | Remove domain-specific language |
| `required_inputs` | ❌ Domain-Specific | Yes | Replace domain-specific inputs with generic equivalents |
| `allowed_models` | ⚠️ Mixed | Yes | Replace concrete model IDs with templates or wildcards |
| `runner_image` | ✅ Generic | No | Keep per-domain runners |
| `evaluator_type` | ❌ Domain-Specific | Yes | Replace domain-specific evaluators with generic ones |
| `expected_artifacts` | ✅ Generic | Yes | Wrap in `artifact_contract` with versioning |
| `resource_profile` | ✅ Generic | No | Keep as-is |
| `approval_tier` | ✅ Generic | No | Keep as-is |
| `execution_status` | ✅ Generic | Yes | Mark legacy entries as deprecated/disabled |
| `submission_backend` | ✅ Generic | No | Keep as-is |
| `execution_blockers` | ✅ Generic | No | Keep as-is |
| `runtime_requirements` | ❌ Domain-Specific | Yes | Remove modalities list; keep only hard constraints (gpu, python_version) |

---

## New Field Additions Summary

| New Field | Type | Default | Purpose | Migration Priority |
|-----------|------|---------|---------|-------------------|
| `experiment_type` | `str` (enum) | `"custom"` | Classify workload without new workflow definitions | HIGH |
| `workload_id` | `str` (auto) | Auto-generated | Unique workload identifier | MEDIUM |
| `schema_version` | `str` | `"1.0.0"` | Registry schema versioning | HIGH |
| `artifact_contract` | `dict` | Current `expected_artifacts` | Artifact versioning and validation | HIGH |
| `metric_contract` | `dict` | Empty | Metric semantics and validation | MEDIUM |

---

## Recommended Workflow Registry Structure

### Phase 1: Add New Fields (Current)

```python
# Add to WorkflowRegistryEntry schema

experiment_type: str = Field(default="custom")
workload_id: str | None = Field(default=None)
schema_version: str = Field(default="1.0.0")
artifact_contract: dict[str, Any] = Field(default_factory=...)
metric_contract: dict[str, Any] = Field(default_factory=...)
```

### Phase 2: Update Existing Entries

Add new fields to all four existing definitions with:
- `schema_version: "1.0.0"`
- `artifact_contract` wrapping current `expected_artifacts`
- `metric_contract` with minimal content
- `experiment_type` mapping (see above)

### Phase 3: Add Generic Entry

Add single `experiment-run` entry with:
- Generic inputs (`dataset_uri`, `model_spec`, `experiment_spec`)
- Wildcard `allowed_models: ["*"]`
- Generic `artifact_contract` and `metric_contract`
- `experiment_type` field to select workload subtype

### Phase 4: Deprecate Legacy Entries

Mark legacy entries as:
```json
{
  "execution_status": "disabled",
  "deprecation_notice": "Use experiment-run with experiment_type={type} instead",
  "migration_guide": "docs/glasslab-v2/workload-migration-examples-2026-04.md"
}
```

---

## References

- Current schema: `/Users/glasslab/cluster-config/services/common/schemas/workflow_registry.py`
- Run artifacts: `/Users/glasslab/cluster-config/services/common/schemas/run_artifacts.py`
- Workflow registry definitions: `/Users/glasslab/cluster-config/services/workflow-registry/definitions/`
- Documentation: `/Users/glasslab/cluster-config/docs/glasslab-v2/workflow-registry.md`
- Related audits:
  - `generic-experiment-gap-audit-2026-04.md`
  - `artifact-contract-audit-2026-04.md`
  - `services.md`

---

*Document generated: 2026-04-22*  
*Repository: `/Users/glasslab/cluster-config`*  
*Service: `workflow-registry`*
