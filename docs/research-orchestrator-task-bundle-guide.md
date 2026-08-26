# Authoring Research Task Bundles (`problem.md`)

This is the operator-facing guide for writing a task bundle that the
research-orchestrator can compile, preflight, and run. It complements the
contract in [`research-orchestrator.md`](research-orchestrator.md) ("Compiled
Research Tasks") with the practical rules that make a bundle pass.

## The bundle contract

A task ZIP must contain:

- **exactly one `problem.md`** — the task specification (this guide);
- **zero or one `eval_agent_prompt.md`** — optional evaluator/rubric guidance.

The archive filename has no meaning. Import rejects links, unsafe paths, and
oversized archives (16 MB zip, 256 files, 64 MB expanded).

At compile time Honeydew reads `problem.md` and produces a `glasslab-task-spec-v1`:
a human-facing name, one approved runtime profile (CPU or GPU), dataset
references, required metric keys and evidence artifacts, and any unresolved
inputs. Honeydew **never invents** images, commands, resources, or Kubernetes
fields — and it is instructed to put anything it cannot resolve into
`missing_inputs`. Preflight then **fails closed** with exactly those items.

**Therefore: every requirement your run depends on must be written in
`problem.md`. If it is not there, the compile will not guess it.**

## Mandatory sections

### 1. Objective

One bounded, specific question. "Train a model on cifar100" is not bounded;
"train a ResNet-18 with triplet + supervised-contrastive losses on CIFAR-100
seen classes and report unseen-class generalization" is.

### 2. Inputs (datasets)

- **Preferred:** upload datasets separately with `/dataset-upload` (or the
  datasets import API) and reference the returned URI in `problem.md`:

  ```text
  glasslab-dataset://<sha256>
  ```

  The compiler resolves this to an immutable `s3://...` binding; preflight
  verifies the file and digest. No download happens at compile time.

- **Remote assets:** declare a canonical public HTTPS URL only when it is
  fast and small. The compiler downloads it with size/address checks, a
  bounded timeout (default 300s) and limited retries. **Do not rely on remote
  downloads for large datasets** — the cifar100 canonical host serves at
  ~100 KB/s from the cluster, so a 160 MB archive takes ~24 minutes and
  exceeds any practical compile timeout. Large data must be uploaded first.

### 3. Method and architecture

State the exact procedure the implementation must follow: model/backbone,
losses, training procedure. "Use a backbone" fails; "ResNet-18, embedding dim
128, batch-hard triplet sampling" passes.

### 4. Hyperparameter search space (exact)

List the space or the fixed values, exactly. Ranges must be concrete:

```text
- Triplet loss margin: {0.1, 0.2, 0.3}
- SupCon temperature: {0.07, 0.1}
- Optimizer: AdamW, learning rate {1e-4, 3e-4}, weight decay 1e-4
- Batch size: 128; epochs: 100
```

### 5. Evaluation rubric (exact)

This is the section preflight enforces hardest. It must contain:

- **Exact metric keys** — the names the evaluator will check (e.g.
  `test_seen_accuracy`, `nmi`, `silhouette_score`).
- **Metric thresholds** — pass/fail values ("pass iff
  `test_unseen_accuracy >= 0.45` AND `mean_recall_by_group >= 0.5`").
- **Stopping conditions** — when the run stops ("after the approved matrix
  completes; at most one run per seed; early stop when validation loss has
  not improved for 10 epochs").
- **Confidence/statistical method, if claims are made** — e.g. "effect-size
  claims require confidence level 0.95 via paired bootstrap with 1000
  resamples, reporting the interval".
- **Required paired-test methodology, if comparisons are made** — e.g. "seen
  vs unseen groups are compared with a paired test on the same seeds".

### 6. Evidence artifacts (required)

Name the files the evaluator and report need:

```text
metrics.json, report.md, plots/, tables/, source.zip
```

## A passing template (representation-learning benchmark)

```markdown
# CIFAR-100 Unseen-Class Representation Generalization

## Objective
Train a representation model on CIFAR-100 seen classes with triplet +
supervised-contrastive losses, then evaluate generalization to unseen classes.

## Inputs
- Dataset: `glasslab-dataset://<sha256>`  (upload cifar100 via /dataset-upload)

## Method and architecture
- Backbone: ResNet-18 (exact), final embedding dim 128.
- Losses: batch-hard triplet loss + supervised contrastive (SupCon).

## Hyperparameter search space (exact)
- Triplet margin: {0.1, 0.2, 0.3}; sampling: batch-hard.
- SupCon temperature: {0.07, 0.1}.
- Optimizer: AdamW, lr {1e-4, 3e-4}, weight decay 1e-4.
- Batch size: 128; epochs: 100.
- Early stopping: no validation-loss improvement for 10 epochs.

## Evaluation rubric (exact)
- Required metric keys: `test_seen_accuracy`, `test_unseen_accuracy`,
  `mean_recall_by_group`, `nmi`, `silhouette_score`.
- Pass thresholds: `test_unseen_accuracy >= 0.45` AND
  `mean_recall_by_group >= 0.5`.
- Stopping condition: stop after the approved matrix completes; at most one
  training run per seed.
- Effect-size claims require confidence level 0.95 with a paired bootstrap
  (1000 resamples); report the confidence interval.

## Evidence artifacts (required)
- `metrics.json`, `report.md`, `plots/`, `tables/`, `source.zip`.
```

## Rules of thumb

1. **Every claim gets an exact number.** "Use contrastive learning" fails;
   "SupCon temperature ∈ {0.07, 0.1}" passes.
2. **Name exact metric keys** — they must match what the evaluator checks.
3. **Large datasets go through `/dataset-upload`**, never a remote URL.
4. **State stopping conditions explicitly.** "Stop after X" beats "run until
   done".
5. **Keep the archive self-contained** — no secrets, private URLs, or
   non-public hosts (preflight rejects them).