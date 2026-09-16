# Learning Task Flow

Status: current operator map

Date: 2026-07-23

This page is the short map for people trying to answer:

> Where do learning tasks live, and how do they actually run on the lab cluster?

The repo has accumulated several valid concerns: physical lab provisioning,
Kubernetes operations, research planning, chat commands, experiment execution,
model serving, and workload-specific ML code. Those should not all feel like
equivalent entry points.

## Three Layers

Think of Glasslab as three layers.

### 1. Physical lab and cluster substrate

Owned by `cluster-config`.

Use this layer when the question is about machines, boot, Kubernetes, storage,
networking, GPU readiness, or deployment.

Relevant paths:

- `ansible/`
- `kubeadm/`
- `kubeadm/glasslab-v2/`
- `scripts/`
- `docs/glasslab-v2/runbooks/`

Canonical live machine:

- `.44` is the provisioner and source of actual live cluster state.

This layer should not encode research-specific behavior except through generic
runtime primitives such as Jobs, PVCs, image pull secrets, service accounts, and
resource labels.

### 2. Glasslab run control plane

Owned by `cluster-config`.

Use this layer when the question is about sessions, approved workloads, run
records, schedules, evaluation, comparison, or artifact indexing.

Relevant paths:

- `services/workflow-api/`
- `services/workflow-registry/`
- `services/evaluator/`
- `services/reporter/`
- `docs/glasslab-v2/generic-experiment-contract.md`
- `docs/glasslab-v2/run-fabric-design-2026-04.md`

The control plane's job is to turn an approved run request into one bounded
Kubernetes Job and a durable record.

At the product layer, an investigation now binds the question and hypotheses to
an immutable plan approval, the resulting runs, and evidence-backed claims. See
[Investigation API v1](investigation-api-v1.md).

### 3. Scientific workload repositories

Owned by each workload repo.

For metric learning, the workload repo is `glasslab-metric-search`.

That repo owns:

- datasets and split code
- model, loss, miner, trainer, and evaluator code
- workload configs
- `RunSpec` shape
- output bundle contents
- workload image build context

It should not own:

- Kubernetes topology
- WhatsApp or command routing
- lab node selection policy
- global run records
- campaign decisions across multiple runs

## Current Supported Lane

For a novel investigation, the supported lane is:

```text
researcher or OpenCode
        |
        v
investigation + digest-pinned execution graph
        |
        | explicit plan approval
        v
research-workspace-cpu-v1 registry definition
        |
        | creates one Kubernetes Job
        v
CPU workspace runner pod
        |
        | verifies task/source digests and executes one command
        v
/mnt/artifacts/{run_id}/
        |
        | complete terminal bundle is ingested
        v
investigation run lineage + evidence-backed claims
```

The important investigation contracts are:

- `POST /investigations/{investigation_id}/plans`
- `POST /investigations/{investigation_id}/plan-approvals`
- `POST /investigations/{investigation_id}/runs`
- `experiment_type`: `research-workspace-job`
- `workload_id`: `research-workspace-cpu-v1`
- task, source, and dataset inputs: immutable URI plus SHA-256
- budget, outputs, and evaluator: explicit parts of the frozen plan

`POST /experiments/runs` remains the lower-level generic run primitive.

For the specialized metric-learning workload, `metric-search-v0` remains a
direct GPU lane:

The current helper for this lane is:

```bash
./scripts/submit-learning-task.sh "Run a bounded metric-search baseline"
```

It is intentionally a thin wrapper around `POST /experiments/runs`, not a
second scheduler.

`workflow-api` then creates a Kubernetes Job using
`services/workflow-api/app/job_submission.py`.

For generic experiment manifests, that Job receives:

- `GLASSLAB_RUNNER_MANIFEST_JSON`
- `GLASSLAB_GENERIC_CONFIG_JSON`
- `GLASSLAB_GENERIC_DATASET_BINDINGS_JSON`
- `GLASSLAB_GENERIC_BUDGET_JSON`
- `GLASSLAB_GENERIC_METRIC_CONTRACT_JSON`
- `GLASSLAB_DATASET_ROOT`
- `GLASSLAB_RUNNER_ARTIFACTS_ROOT`

The workload container should read those inputs, run one bounded candidate, and
write a terminal bundle under the artifacts mount.

The research workspace runner validates those bindings and exposes the
resolved paths to the candidate as:

- `GLASSLAB_DATASET_BINDINGS_JSON`, the canonical JSON object
- `GLASSLAB_DATASET_<NORMALIZED_NAME>`, a namespaced per-binding variable
- `<NORMALIZED_NAME>_PATH`, a compatibility alias for imported benchmark code

For example, the binding `adult_train` is available as both
`GLASSLAB_DATASET_ADULT_TRAIN` and `ADULT_TRAIN_PATH`. Binding names that would
collide after normalization are rejected.

## What Lives Where

### `cluster-config`

Use this repo for:

- live cluster operations
- Kubernetes manifests
- workflow API code
- workload registry entries
- evaluator and reporter services
- run submission and ingestion
- deployment and smoke-test scripts
- current-state docs

Primary files for metric-search execution:

- `services/workflow-registry/definitions/metric-search-v0.json`
- `services/workflow-api/app/job_submission.py`
- `services/workflow-api/app/autoresearch.py`
- `docs/glasslab-v2/generic-experiment-contract.md`
- `docs/glasslab-v2/autoresearch-lane.md`

### `glasslab-metric-search`

Use this repo for:

- the metric-learning workload implementation
- configs and search spaces
- dataset protocols
- training and evaluation scripts
- metrics schema emitted by the workload
- workload image build

Primary files for cluster execution:

- `scripts/run_experiment.py`
- `docs/run-spec.md`
- `docs/glasslab-integration.md`
- `Dockerfile`

## Secondary Or Historical Paths

These paths may still exist, but they should not be the default route for new
learning tasks.

### Titanic v1 agent stack (removed)

The v1 agent stack, fixed Titanic runner, and its manifests have been removed
from this repository (issues #157/#158). Do not use it as the template for new
learning tasks.

### Direct Kubernetes Jobs

Use only for debugging and bring-up.

If an ad hoc Job produces a result worth keeping, backfill it into the
`workflow-api` record path or rerun it through the generic experiment endpoint.

### OpenClaw

OpenClaw is not the current command-critical path.

The current command direction is:

```text
whatsapp-gateway -> research-ingress -> research-command-router -> workflow-api
```

### Exo / Mac inference

The Macs provide an OpenAI-compatible inference service for local model help.

They are not Kubernetes workers and should not be treated as the learning-task
scheduler.

## Simplified Operating Rule

When someone asks "how do I run a learning task?", answer with one path:

1. Put workload-specific code and configs in the workload repo.
2. Register the workload once in `services/workflow-registry/definitions/`.
3. Submit runs through `POST /experiments/runs`.
4. Let `workflow-api` create the Kubernetes Job.
5. Write artifacts to `/mnt/artifacts/{run_id}/`.
6. Ingest metrics and compare through the evaluator/autoresearch contract.

Do not make the operator choose between the v1 agent API, direct manifests,
OpenClaw, notebooks, and generic experiments as equal options.

## Near-Term Simplification Work

The repo is conceptually cleaner than its file tree suggests, but the operator
surface still needs tightening.

Recommended next changes:

- keep one registry entry per workload instead of new API families
- require explicit evaluator contracts for automatic keep/discard decisions
- document artifact bundle shape once and make workloads conform to it
- move old direct-run examples under historical or debug wording
- keep `.44` as the source of actual live state and GitHub as committed state

The target is not fewer directories. The target is fewer competing execution
stories.
