# Specification Fields: `problem.md` -> task spec -> evaluation contract -> matrix

Audit for issue [#492](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/492).

This document answers four questions in order of usefulness to an author:

1. [How to author a `problem.md`](#1-how-to-author-a-problemmd) - the practical answer.
2. The field inventories: [`problem.md`](#2-problemmd-field-inventory),
   [task spec](#3-task-spec-glasslab-task-spec-v1), and
   [evaluation contract](#4-evaluation-contract-descriptor).
3. [Resource authority](#5-resource-authority-profiles-precedence-and-the-recommended-rule) -
   why fixed profiles exist, and the one precedence rule.
4. [Validation layers](#6-validation-layers) and
   [findings](#8-findings-and-defects).

Every row below is grounded in the current `testing` tree with `file:line`
evidence. Where a rule exists **only in an agent prompt** it is marked
`[PROMPT-ONLY]`; those are the fragile spots, because no validator enforces them
and a model change silently changes behavior.

> **Scope caveat.** This is a docs-only audit. The cluster was unreachable, so
> nothing here was executed live. The deterministic gates (Pydantic validators,
> `preflight.py`, `contracts.py`, `methodology_requirement_validation.py`) are
> read from source and are exact. The compile step is model-driven and
> non-deterministic; statements about what the compiler *will* produce are marked
> as prompt behavior, not guarantees.

---

## 1. How to author a `problem.md`

**The one thing to internalize:** the orchestrator never parses your
`problem.md` for sections or fields. It only checks that the ZIP contains
**exactly one file named `problem.md`** (`services/research-orchestrator/app/task_bundles.py:411-425`)
and that it decodes as UTF-8 (`task_bundles.py:445-452`). Everything else in the
document is read by a model (Honeydew) and compiled into a typed
`glasslab-task-spec-v1` proposal; the typed proposal is what validators check.
There is no markdown/frontmatter parser and no heading schema anywhere in the
service (no markdown parser is even a dependency).

Consequently the guide's "mandatory sections" are **conventions the compiler
prompt relies on**, not structural requirements. A `problem.md` with the right
prose in unlabeled paragraphs can compile; a `problem.md` with perfect headings
but vague numbers will be parked in `missing_inputs`. The hard gates start after
compilation, on the task spec, the contract, and the matrix.

Practical rules that actually gate a run:

- **State exact metric keys** as they will appear at the **root** of
  `metrics.json`. The evaluator compares `task_spec.required_metric_keys` against
  the top-level keys of `metrics.json`
  (`evaluation-contracts/generic-task-integrity-v1/1.0.0/evaluator.py:43-46`).
  For a task-specific contract the keys live in
  `manifest.required_metric_keys` and are checked statically at matrix preflight
  (`preflight.py:534-538`).
- **Name exact evidence artifacts** as relative paths. They are unioned with the
  base set (`task_bundles.py:140-150`, `534-541`) and each must be statically
  referenced by a string literal in scanned source (`preflight.py:373-390`).
- **Use one dataset declaration form per asset**: either a pre-uploaded
  `glasslab-dataset://<64-hex-sha256>` **or** a public HTTPS `source_url` - never
  both (`schemas.py:254-262`).
- **Do not write a `container image`, `command`, `workflow_id`, `resources`, or
  Kubernetes fields.** The compiler prompt forbids inventing them
  ([PROMPT-ONLY], `engine.py:711-714`) and the schema has no field for them
  anyway: `TaskSpecProposal` (`schemas.py:279-310`) cannot carry them.
- **Wall-clock budget**: whatever you write does not override the preselected
  profile. If you write "30 minutes" and the compiler picks
  `cpu-ml-standard-v1` (60), the matrix must still request 60. See
  [section 5](#5-resource-authority-profiles-precedence-and-the-recommended-rule).

See [section 7](#7-minimal-viable-problemmd) for a complete worked example.

---

## 2. `problem.md` field inventory

### 2.1 What is structurally parsed

| Field / rule | Purpose | Required | Validated where | Consumes it | Failure mode |
|---|---|---|---|---|---|
| File named `problem.md` | The task specification | Yes | compile: `task_bundles.py:411-415`, `421-425` | compiler session workspace (`task_bundles.py:429-437`) | `TaskBundleError: task archive requires one problem.md and at most one eval_agent_prompt.md`. Non-retryable, fail-closed. |
| UTF-8 decodability | Text input | Yes | compile: `task_bundles.py:445-452` | compiler | `TaskBundleError: problem and evaluator prompt must be UTF-8 text`. Fail-closed. |
| Archive size / file count / path safety | Bound the upload | Yes | compile: `task_bundles.py:346-348`, `377-425` | importer | `task archive has an invalid size` / `file count is invalid` / `unsafe task archive member` / `expands too large`. Fail-closed. |

**There are no other structural fields.** No heading, key, or YAML/JSON block in
`problem.md` is parsed. The guide's six "mandatory sections"
(`docs/research-orchestrator-task-bundle-guide.md:30-96`) are not enforced by any
validator.

### 2.2 What is agent-interpreted

All of the following are read by the compiler model and mapped into
`TaskSpecProposal` fields. The mapping is specified **only in the compile prompt**
(`engine.py:701-718`) and the guide; the JSON-schema-forced structured output of
the model is `AgentTurnResult` (`schemas.py:313-327`). There is no deterministic
check that a given problem.md section exists.

| Guide section (guide lines) | Feeds task-spec field | Where the rule lives | Failure mode if absent/vague |
|---|---|---|---|
| Objective | `display_name`, `rationale`, and the run objective | `[PROMPT-ONLY]` `engine.py:701-718` | Model omits or invents; `display_name` must be 3-160 chars (`schemas.py:285`). Vague objective survives compile but fails methodology review later. |
| Inputs / datasets | `assets[]` (`TaskAssetProposal`) | `[PROMPT-ONLY]` `engine.py:707-712` + schema `schemas.py:231-276` | No assets → run starts with no data bindings; workload fails at execution. A nonpublic/private URL is rejected by the fetcher. |
| Method and architecture | `rationale`, and later `program.md` / implementation | `[PROMPT-ONLY]` | Not compiled into a typed field; becomes protocol prose. No deterministic gate. |
| Hyperparameter search space | Later matrix `base_config` + `overrides`; contract `methodology_requirements` | `[PROMPT-ONLY]`; enforced only once a contract declares `config_path`s (`preflight.py:481-518`) | If the contract declares a comparison/decision and the values are not materialized under `experiment_dimensions.*`, preflight fails closed. |
| Evaluation rubric → metric keys | `required_metric_keys` | schema `schemas.py:289-291` (**pattern only**); semantic check at execution for the generic contract | Keys are pattern-validated (`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`) but **not** checked against `run.py` statically on the generic path (see [F7](#8-findings-and-defects)). |
| Evaluation rubric → thresholds | Partly `rationale`; really the evaluator contract | `[PROMPT-ONLY]`; deterministic only for task-specific contracts | Thresholds are not a task-spec field. They must become contract logic, or they are prose. |
| Evaluation rubric → stopping conditions | Contract `manifest.budget` / matrix seeds | `[PROMPT-ONLY]` | Not compiled; the profile wall-clock is authoritative. |
| Evidence artifacts | `required_artifacts` | schema path-safety `schemas.py:295-310`; existence at preflight `preflight.py:373-390` | Unsafe path → compile rejection. Missing static reference → matrix preflight error (retryable). |

**Boundary summary:** the only deterministic/agent boundary in `problem.md` is
"is it exactly one UTF-8 file of the right size". Everything semantic is on the
agent side and is re-validated only after it has been compiled into a typed
proposal. An author cannot rely on the document alone to be authoritative; the
authoritative artifact is the compiled `TaskBundleRecord`
(`task_bundles.py:46-71`).

### 2.3 Datasets (`glasslab-dataset://` vs public HTTPS vs catalog)

| Form | Field | Where | Tradeoffs / limits |
|---|---|---|---|
| Pre-uploaded dataset | `approved_uri` matching `^glasslab-dataset://[a-f0-9]{64}$` (`schemas.py:237-240`) | Registry resolution `task_bundles.py:494-516`; preflight re-hash `task_bundles.py:611-628` | Preferred. No compile-time download. Digest verified at upload and again at task preflight. Upload ceiling 2 GiB HTTP / 100 MiB Discord (`config.py:121-122`; `command-surface.md:198-202`). |
| Public HTTPS URL | `source_url` + `expected_sha256` (`schemas.py:236`, `241-251`) | Fetch `task_bundles.py:288-342`; checksum established by orchestrator `engine.py:297-342` | Best-effort only. Compiler fetch is bounded (16 MiB ZIP is irrelevant here; asset cap 2 GiB, `config.py:114`), with per-hop public/global-address checks, redirect rejection, 300 s default read timeout (`task_bundles.py:357-360`). The guide warns large hosts (cifar100 at ~100 KB/s) exceed practical compile timeout (`task-bundle-guide.md:50-54`). |
| Catalog entries | `CatalogDatasetRecord` (`schemas.py:791-824`) | Dataset registry | Curated wrapper over the content-addressed registry with a stable name; not referenced directly from `problem.md` (the compiler resolves to `approved_uri`). |

An asset may not carry both `source_url` and `approved_uri`
(`schemas.py:259-262`). `expected_sha256` is **required at submission time** for
`source_url` assets (`schemas.py:266-275`) but is populated by the orchestrator
from the fetched bytes, not by the model (`engine.py:297-342`); see
[F2](#8-findings-and-defects) for the history (#487) and
[F3](#8-findings-and-defects) for the read-time fix (#480).

### 2.4 Metrics and thresholds across the three layers

Three distinct concepts share the word "metric":

| Concept | Location | Owner | Consumed by |
|---|---|---|---|
| `required_metric_keys` (task spec) | `schemas.py:289-291` | compiler (from rubric) | Generic evaluator at execution: `generic-task-integrity-v1/1.0.0/evaluator.py:43-46`, read from `task_spec.required_metric_keys`. |
| `manifest.required_metric_keys` (contract) | free-form `manifest` dict (`schemas.py:480`) | contract author (Honeydew proposes; human promotes) | Static matrix preflight `preflight.py:534-538` → `_metrics_root_errors` requires `run.py` to serialize those root keys. |
| `manifest.primary_metric` / `primary_metric_direction` | `schemas.py:480` | contract author | Contract validation `contract_candidates.py:161-168`; job `metric_contract` (`cluster.py:289-296`); evaluator output. |

They are **not** automatically reconciled. For the generic path the contract's
list is empty (`generic-task-integrity-v1/1.0.0/contract.json`), so the static
check is a no-op and the task-spec keys are enforced only after the job runs
([F7](#8-findings-and-defects)). For a task-specific contract the two lists
should match, but nothing asserts that; the contract's list wins for preflight
while the task spec's list still travels in the job payload
(`cluster.py:262-263`).

---

## 3. Task spec (`glasslab-task-spec-v1`)

Two distinct objects share this name:

- **`TaskSpecProposal`** - what the compiler model proposes (`schemas.py:279-310`).
  `schema_version` is the literal `glasslab-task-spec-v1`.
- **`TaskBundleRecord`** - the compiled, immutable policy output
  (`task_bundles.py:46-71`, `schema_version: glasslab-task-bundle-v2`).

The table lists the fields named in the audit request. "Owner" distinguishes
model-proposed from policy-derived.

| Field | Owner | Required | Where validated | Consumer | Failure mode |
|---|---|---|---|---|---|
| `display_name` | model | Yes, 3-160 chars (`schemas.py:285`) | compile (Pydantic) | task id derivation ignores it (`task_bundles.py:462-468`); display | Too short/long → compile rejection; retryable via model redraft. |
| `runtime_profile` | model | Yes, `Literal['cpu-ml-standard-v1','gpu-ml-standard-v1']` (`schemas.py:286`) | compile (Pydantic) | `RUNTIME_PROFILES` lookup `task_bundles.py:481` | Unknown value → compile rejection. |
| `assets[]` | model | Optional, default `[]` (`schemas.py:287`) | compile (Pydantic + custom) | `compile()` resolves each asset `task_bundles.py:492-530`; unresolved → `missing_inputs` | Unresolvable asset → recorded in `missing_inputs`, task preflight blocks (`task_bundles.py:587-641`). |
| `required_artifacts` | model | Optional, default `[]` (`schemas.py:288`); path-safety validator `schemas.py:295-310` | compile | unioned with `BASE_REQUIRED_ARTIFACTS` `task_bundles.py:534-541` | Unsafe path → ValueError. Missing artifact not statically referenced → matrix preflight error (retryable). |
| `required_metric_keys` | model | Optional, default `[]` (`schemas.py:289-291`); name pattern only | compile | generic evaluator at execution | Malformed name → ValueError. Semantics not statically checked on generic path ([F7](#8-findings-and-defects)). |
| `missing_inputs` | model | Optional, default `[]` (`schemas.py:292`) | compile | `TaskPreflight.missing_inputs`; blocks `ready` `task_bundles.py:594`, `629-639` | Any entry → task cannot start; `spec_feedback.format_spec_feedback` renders a human message. Non-retryable until the input is supplied. |
| `rationale` | model | Yes, min length 1 (`schemas.py:293`) | compile | audit only | Empty → compile rejection. |
| `resources` (compiled) | **policy** (profile) | Always present | derived at compile `task_bundles.py:571`; exact-match enforced at matrix preflight `engine.py:5046-5050` | matrix `resources`; job spec | Any matrix value != profile → matrix preflight error. See [section 5](#5-resource-authority-profiles-precedence-and-the-recommended-rule). |
| `workload_id` | **policy** | Always | derived `task_bundles.py:564` from the profile (`workspace-cpu-ml-v1` / `workspace-gpu-ml-v1`) | workflow-api submission `cluster.py:236` | Fixed; not model-selectable. Registry entry must exist. |
| `experiment_type` | **policy** | Always | `'research-workspace-job'` `task_bundles.py:565` | workflow-api submission `cluster.py:235` | Fixed. |
| `command` | **policy** | Always | `['python3','run.py']` `task_bundles.py:567` | workspace `command` in submission `cluster.py:257` | Fixed. Validated by workflow-api (`schemas.py:1142-1153`: executable must be `python3`, ≤64 args). |
| `source_subdirectory` | **policy** | Always | `research-workspace/<task-id>` `task_bundles.py:568` | source-bundle packaging `engine.py:5466-5470`; preflight containment `preflight.py:525-531` | Escaping the workspace is rejected. |
| `runner_image` | **policy** | Always | profile image `task_bundles.py:566`; re-bound on load `task_bundles.py:670-672`; allowlist at task preflight `task_bundles.py:596` | cluster job image | Not allowlisted → task preflight blocker `compiled runtime image is not permitted`. |
| `default_contract_id` / `default_contract_version` | **policy** | Always | `generic-task-integrity-v1` / `1.0.0` `task_bundles.py:569-570` | contract resolution `engine.py:742-755` | Missing/unresolvable → `evaluator_ready=False` → task preflight blocker. |

Note the task spec has **no** `command`, `workflow_id`, `experiment_type`,
`resources`, or `source_subdirectory` field in `TaskSpecProposal`. Those exist
only on the compiled record and are policy output; this is deliberate
(`task_bundles.py:47`).

---

## 4. Evaluation contract descriptor

Descriptor schema: `EvaluationContractDescriptor` (`schemas.py:475-487`).
A contract is a directory `contract_id/version/` containing `contract.json`, a
wrapper, an evaluator, input/output JSON schemas, and `contract.sha256`
(repository contracts: `evaluation-contracts/<id>/<version>/`). The digest is
`sha256` over the framed contents of every non-checksum, non-pycache file
(`contracts.py:38-69`).

| Field | Owner | Required | When written | Validated where | Consumed by | Failure mode |
|---|---|---|---|---|---|---|
| `contract_id` | contract author | Yes, min 3 (`schemas.py:478`) | seal/promote or repo install | identity match candidate path `contract_candidates.py:150-153`; resolver path `contracts.py:112-113` | job labels, catalog key `contract_candidates.py:382-392` | Mismatch → `contract identity does not match its path`. Fail-closed. |
| `version` | contract author | Yes, min 1 (`schemas.py:479`) | same | same | catalog/resolution | same. |
| `manifest` | contract author | Yes, dict (`schemas.py:480`) | same | free-form; individual keys checked (below) | preflight, evaluator | Malformed keys → seal rejection. |
| `manifest.primary_metric` | contract author | Yes (semantic) | seal | `contract_candidates.py:161-168` | proposal compatibility `engine.py:3628-3635`, `2578` | Missing/empty → `manifest requires primary_metric and a valid direction`. |
| `manifest.primary_metric_direction` | contract author | Yes, `maximize`/`minimize` | seal | `contract_candidates.py:162-168` | same | Invalid direction → seal rejection. |
| `manifest.methodology_requirements` | contract author | Optional, default `[]` | seal | see [4.1](#41-methodology_requirements) | matrix preflight | Invalid → seal rejection (`ContractCandidateError`). |
| `manifest.budget` | contract author | Optional (free-form) | seal | not validated at seal for shape | cluster `budget` comes from `spec.resources.wallclock_minutes`, not this (`cluster.py:279-281`) | Descriptor `budget` is largely inert on the current path. |
| `manifest.guardrails` | contract author | Optional | seal | not validated at seal |
| `execution_wrapper` | contract author | Yes, min 1 | seal | referenced file must exist + AST-parse `contract_candidates.py:170-204` | workflow-api job render |
| `evaluation_entry_point` | contract author | Yes, min 1 | seal | same | evaluator invocation | Missing file → `candidate references missing file`. Fail-closed. |
| `expected_input_schema` | contract author | Yes, min 1 | seal | file exists + JSON object `contract_candidates.py:182-190` | evaluator |
| `expected_output_schema` | contract author | Yes, min 1 | seal | same | evaluator |
| `required_artifacts` | contract author | Yes, min 1 (`schemas.py:485`) | seal | non-empty enforced by Pydantic | unioned into job artifacts `matrix.py:57-66`; static source check `preflight.py:540` | Empty → seal rejection. |
| `resource_constraints` | contract author | Yes (`ResourceRequest`, `schemas.py:486`) | seal/promote | must contain the task profile at seal/preflight (`engine.py:4956-4991`); matrix ≤ constraints `matrix.py:47-55` | profile-vs-contract conflict gate | profile > constraints → non-retryable human pause. See [section 5](#5-resource-authority-profiles-precedence-and-the-recommended-rule). |
| `container_image_digest` | **must be null for candidates** | Optional (`schemas.py:487`) | seal | candidates must set null `contract_candidates.py:154-160`; repo contracts may pin | `contracts.render_read_only_contract_job` (review path) | Non-null in a candidate → seal rejection `shared-bundle candidates cannot choose a container image`. |

Contract lifecycle: seal -> Honeydew review -> human promotion -> binding ->
reuse (`contract_candidates.py:206-311`, `engine.py:3898-3958`). A promoted
`id/version` is immutable: a second digest is refused
(`contract_candidates.py:291-296`). `contract.sha256` is written by the
orchestrator, never the agent (`contract_candidates.py:230-233`), and re-verified
at resolution (`contracts.py:126-134`).

### 4.1 `methodology_requirements`

Schema: `MethodologyRequirement` (`preflight.py:34-42`):

| Field | Required | Meaning |
|---|---|---|
| `requirement_id` | Yes | Unique per contract (`methodology_requirement_validation.py:117-121`). |
| `config_path` | Yes | Dotted key into the matrix `base_config` YAML, rooted at `experiment_dimensions` (`preflight.py:93`, `methodology_requirement_validation.py:64-77`). |
| `mode` | Yes, `decision` or `comparison` (`preflight.py:39`). |
| `minimum_distinct_values` | default 1, ge 1. |
| `maximum_distinct_values` | optional, ge 1. |
| `description` | non-empty (`methodology_requirement_validation.py:125-129`). |

Worked example - `comparison` (from the Adult contract,
`evaluation-contracts/ml-benchmark-adult-income-v1/1.1.0/contract.json`):

```json
{
  "requirement_id": "model_families",
  "config_path": "experiment_dimensions.model",
  "mode": "comparison",
  "minimum_distinct_values": 2,
  "description": "Compare at least one linear and one non-linear model family."
}
```

The matrix `base_config` must then contain a list with at least two distinct
values at exactly `experiment_dimensions.model`, e.g.
`experiment_dimensions: {model: [logistic-regression, random-forest]}`.

Worked example - `decision` (same contract):

```json
{
  "requirement_id": "missing_data_strategy",
  "config_path": "experiment_dimensions.missing_strategy",
  "mode": "decision",
  "minimum_distinct_values": 1,
  "maximum_distinct_values": 1,
  "description": "Choose and justify one explicit missing-data strategy."
}
```

The config must contain exactly one scalar at that path, e.g.
`experiment_dimensions: {missing_strategy: median-imputation}`.

**Exactly what deterministic code asserts, and where:**

*At seal/promotion* (`contract_candidates.py:35-59`, `169`, `285`;
`methodology_requirement_validation.py:133-142`):
`config_path` is a dotted key path, not a filesystem path
(`methodology_requirement_validation.py:20-23`); no `/`, `\`, leading/trailing
dot, empty segments, or `..`; must be rooted at `experiment_dimensions` with at
least 2 segments; `requirement_id` non-empty and unique; `description` non-empty;
`maximum_distinct_values >= minimum_distinct_values`; `comparison` requires
`minimum >= 2`; `decision` requires `minimum == 1`.

*At matrix preflight* (`preflight.py:468-523`, `567-572`):
the key resolves from the base_config root (`_config_value`,
`preflight.py:96-102`); a missing key yields
``missing methodology setting `<path>`: <description>``; a mapping value yields
`` `<path>` must directly contain a scalar or list of values, not a metadata
object; do not wrap values beneath `description` or `values` ``; the distinct
string count must be within `[minimum, maximum]`; comparison entries land in
`comparisons`, decisions in `decisions`; and if **any** requirement is
`comparison`, the matrix must have at least `MIN_COMPARISON_SEEDS = 3` seeds
(`schemas.py:73`, `preflight.py:567-572`).

The prompt guidance that teaches this to Honeydew/Beaker is
`METHODOLOGY_REQUIREMENTS_GUIDANCE` (`engine.py:133-153`) and
`MATRIX_VARIANT_RULES_GUIDANCE` (`engine.py:160-172`) - both `[PROMPT-ONLY]` (they
mirror the validators, with a drift guard noted at `engine.py:155-159`).

---

## 5. Resource authority: profiles, precedence, and the recommended rule

### 5.1 The runtime profiles

Two profiles exist, both defined in code in
`services/research-orchestrator/app/task_bundles.py:96-125`:

| Profile | `workload_id` | cpu | memory_gib | gpus | wallclock_minutes | runner image |
|---|---|---|---|---|---|---|
| `cpu-ml-standard-v1` | `workspace-cpu-ml-v1` | 4 | 8 | 0 | 60 | `...runner@sha256:dae5bc49...` |
| `gpu-ml-standard-v1` | `workspace-gpu-ml-v1` | 8 | 32 | 1 | 240 | `...runner@sha256:9e7c18d1...` |

The same values are restated in the workflow registry as Kubernetes
requests/limits: `services/workflow-registry/definitions/workspace-cpu-ml-v1.json`
(requests `cpu 1 / mem 2Gi`, limits `cpu 4 / mem 8Gi`, `max_wallclock_minutes 60`)
and `workspace-gpu-ml-v1.json` (limits `cpu 8 / mem 32Gi / nvidia.com/gpu 1`,
`max_wallclock_minutes 240`). The registry is what workflow-api actually renders
into the Job (`services/workflow-api/app/job_submission.py:602-608`, `707-713`).

Both runner images are in the orchestrator's permitting allowlist in the live
configmap (`kubeadm/glasslab-v2/research-orchestrator/10-configmap.yaml:88`),
though the *code default* `permitted_job_images` lists only the CPU image
(`config.py:243-246`).

**Why do they exist?** The recorded intent is a bounded-execution security
boundary, not a scientific one:

- The compiler "does not select a container image, command, workload ID,
  resource ceiling, evaluator entry point, or Kubernetes fields"
  (`docs/research-orchestrator.md:449-451`).
- The product boundary lists "one approved repository and fixed agent profiles"
  among MVP limitations (`docs/research-orchestrator.md:959`;
  `docs/research-orchestrator-command-surface.md:412`).
- The harness contract gives the workload `network_policy: none`
  (`cluster.py:260`) and runs unprivileged (`job_submission.py:714-717`).

**Is the deeper intent recorded?** No. There is no design doc, ADR, or commit
body explaining why a task cannot determine its own resource envelope. The
profile-introducing commits (`4aaeca5`, `54a5f58`, `8b2197f`) have empty bodies.
That absence is itself a finding ([F9](#8-findings-and-defects)): the current
"profile is the exact matrix value" rule was a consequence of a safety choice,
not a documented resource policy, which is why #483 was possible.

### 5.2 The four authorities, as the code actually behaves

| Authority | Defined at | Current effect |
|---|---|---|
| Task profile | `task_bundles.py:96-125` | **Exact-match authority** for imported tasks: `matrix.resources` must equal the profile dict (`engine.py:5038-5050`). |
| Contract `resource_constraints` | descriptor `resource_constraints` | Compatibility envelope. Profile must fit inside it (`preflight.py:409-438`; `engine.py:4956-4991`), and matrix must not exceed it (`matrix.py:47-55`). If the profile exceeds it, the run fails closed for human resolution. |
| Matrix `resources` | `ExperimentMatrix.resources` (`schemas.py:437`) | Copied by the agent; must equal the profile (imported) and fit the contract and policy. |
| Global policy | `config.py:265-268`; `policy.py:140-156` | Hard clamp evaluated at action classification: `cpu<=8`, `memory_gib<=32`, `gpus<=1`, `parallel<=4`. Also the image allowlist. |

So today, for an **imported** task, precedence is:

> profile (exact for matrix) > contract (must contain profile) > policy (outermost
> hard clamp), with the matrix as a copy of the profile rather than an
> independent choice.

For an **objective-driven** task (no task bundle) there is no profile; the
matrix resources are derived from the contract proposal, clamped to policy
(`engine.py:4506-4531`), and bounded by the contract at expansion
(`matrix.py:47-55`).

### 5.3 Why #483 was unsatisfiable, and why #490 still loops

**#483** arose because the *same run* was subject to two independent ceilings:

- Rule A (contract): `matrix.wallclock_minutes <= contract.wallclock_minutes`
  (`matrix.py:47-55`).
- Rule B (profile): `matrix.wallclock_minutes == profile.wallclock_minutes`
  (`engine.py:5046-5050`).

The Titanic contract declared `wallclock_minutes: 30`
(`resource_constraints {"cpu":8.0,"gpus":0,"memory_gib":32.0,"wallclock_minutes":30}`)
while `cpu-ml-standard-v1` declares `60` (`task_bundles.py:104-109`). With
`60 == 60` required by B and `60 <= 30` required by A, no matrix could satisfy
both: the observed 60↔30 oscillation. The current code resolves it by making the
profile authoritative and turning a profile>contract mismatch into a
**non-retryable** human pause (`engine.py:4956-4991`, `5010-5021`) plus a seal-time
rejection (`engine.py:3567-3612`). That is a correct fail-closed treatment, but
it leaves the *contract* as a second, independent ceiling that can be stricter
than the platform's own profile - the root cause is not eliminated, only
detected.

**#490** is the same contradiction on a different path. When a run binds to an
**already-promoted** contract, `_promote_contract_candidate` calls
`_contract_binding_compatible` (`engine.py:3932`), which re-checks
`proposal.resource_constraints <= installed.resource_constraints`
(`engine.py:2584-2595`). A stale promoted contract (constraints 30) against a
protocol that now carries the authoritative profile (60) returns False and
raises a plain `WorkflowError('installed contract remains incompatible with the
protocol')` (`engine.py:3932-3935`). Unlike the candidate path, this branch does
**not** call `_profile_contract_conflict_message` and does **not** mark the
failure non-retryable, so `resume` re-enters the same branch and returns HTTP 409
forever. The gap is precisely that the profile-vs-contract check was implemented
on the candidate/matrix paths (#483/#484) but not on the installed-contract
binding path ([F6](#8-findings-and-defects), tracked as #490).

### 5.4 Recommended precedence rule (express once)

> **The compiled task's runtime profile is the single authoritative source of a
> job's requested resources. The evaluation contract's `resource_constraints`
> MUST be a superset of the profile (validated once, at contract seal/promotion
> and re-checked at every binding); the matrix MUST copy the profile exactly; and
> global policy is the outermost hard clamp applied when the profile is compiled.
> No other layer may act as an independent ceiling.**

Enforcement point: the only place that should compare profile to contract is
`profile_contract_resource_conflicts` invoked from
`_profile_contract_conflict_message`, and it must be reached from **all** three
entry points - contract draft/seal, matrix preflight, and installed-contract
binding - by routing the binding failure through the same non-retryable human
handler rather than a bare `WorkflowError`.

### 5.5 Should the experiment determine its own resources?

**Recommendation: profiles as envelopes/defaults, not exact values - but only
after the stale-binding path is fixed, and with a human gate on increases.**

Concretely:

- Treat a profile as a **declared envelope** (default request + maximum). A task
  may request any value `<= profile` in each dimension; the matrix is clamped to
  `min(request, profile.envelope, contract.constraints, policy)`.
- A request **above** the profile requires an explicit, recorded human approval
  that names the dimension and both values, and the contract must already allow
  it. Never silently upsize.
- Validate `contract.constraints >= profile.envelope` once, at promotion, so a
  contract can never be the stricter of two ceilings again.

Tradeoffs:

| Dimension | Fixed profiles (today) | Experiment-determined (recommended) |
|---|---|---|
| Scheduling | Predictable bin-packing; profiles map to known nodes. | Variable requests; larger jobs may queue behind capacity and starve small ones. |
| Quotas | Trivially bounded by the clamp. | Needs per-run/per-user quota accounting; easy to under- or over-account. |
| Abuse | A prompt-injected task cannot escalate CPU/GPU. | Escalation is possible unless every increase is human-gated and budgeted. |
| Reproducibility | Every run of a task gets identical resources; reruns comparable. | Runs may differ in resources; results can shift with size/time. |
| Comparability | Benchmarks are apples-to-apples across runs. | Cross-run comparison weakens unless the effective resource tuple is recorded and matched. |

The reproducibility/comparability cost is real but is already paid on the
objective-driven path, where there is no profile and resources come from the
proposal. The safest incremental step is therefore: keep the profile as the
*default and ceiling* for imported benchmarks (preserving comparability), allow
smaller requests, and require a human gate for anything larger. This removes the
#483 exact-match trap without making the platform a resource free-for-all.

---

## 6. Validation layers

Layers: **compile** (task-bundle import + agent-turn schema), **seal**
(contract candidate seal/promotion), **preflight** (task preflight, matrix
preflight, verification preflight), **execution** (action policy, matrix
expansion, cluster submission, job rendering), **read** (stored-payload
revalidation).

Failure classifications: **retryable** = an agent can redraft; **non-retryable**
= configuration contradiction, needs a human; **fail-closed** = refuse and record,
no model retry.

| Field / rule | Layer | Module | Classification | User-visible message |
|---|---|---|---|---|
| `problem.md` present exactly once | compile | `task_bundles.stage_archive` | fail-closed | `task archive requires one problem.md and at most one eval_agent_prompt.md` |
| `problem.md` UTF-8 | compile | `task_bundles.stage_archive` | fail-closed | `problem and evaluator prompt must be UTF-8 text` |
| ZIP size/count/paths | compile | `task_bundles.stage_archive` | fail-closed | `task archive has an invalid size` / `unsafe task archive member` / ... |
| `TaskSpecProposal` shape (`display_name`, `runtime_profile`, `rationale`, patterns) | compile | Pydantic `schemas.py:279-310` via `AgentTurnResult` | retryable | Pydantic errors surfaced; compiler turn retried (`engine.py:721-728`) |
| `source_url` needs `expected_sha256` | compile (preparer) | `engine._establish_source_url_asset_checksums` `engine.py:297-342` | fail-closed (fetch failure) | `source_url asset ... could not be fetched and verified: ...` |
| `source_url` unverifiable / private host | compile | `url_fetch` via `task_bundles` | fail-closed | transport-specific (`task asset redirect was rejected`, `task asset exceeds N bytes`, ...) |
| `approved_uri` digest resolution | compile | `task_bundles.compile` `494-516` | retryable (recorded in `missing_inputs`) | `ingested dataset registry is unavailable: ...` |
| `display_name` / artifact path safety | compile | `schemas.py:295-310` | retryable | `unsafe required artifact path: ...` |
| `runtime_profile` unknown | compile | Pydantic Literal | retryable | Pydantic `Input should be ...` |
| Task archive checksum re-verify | preflight | `task_bundles.preflight:604-610` | fail-closed | `task archive is unavailable or failed checksum verification` |
| Asset present + digest match | preflight | `task_bundles.preflight:611-628` | fail-closed | `asset is unavailable or failed checksum verification: <name>` |
| Runner image allowlisted | preflight | `task_bundles.preflight:596` | fail-closed | `compiled runtime image is not permitted` |
| Evaluator contract installed | preflight | `task_bundles.preflight:598-599` | fail-closed | `compiled evaluation contract is not installed` |
| `missing_inputs` empty | preflight | `task_bundles.preflight:594,629-639` | non-retryable (until input supplied) | `format_spec_feedback(issues)` |
| Matrix `base_config` safe + exists | preflight | `schemas.py:440-454`; `preflight.py:454-458` | retryable | `base_config does not exist inside the Beaker workspace: ...` |
| `variants[].name` pattern/unique | compile | `schemas.py:425`, `463-472` | retryable | Pydantic `String should match pattern '^[a-z0-9][a-z0-9_-]{0,62}$'` |
| `seeds` unique | compile | `schemas.py:456-461` | retryable | `seeds must be unique` |
| Methodology `config_path` present + scalar/list + count | preflight | `preflight.py:481-518` | retryable | `missing methodology setting ...` / `requires at least N distinct value(s); found M` |
| Comparison needs ≥3 matrix seeds | preflight | `preflight.py:567-572` | retryable | `comparison contract requires at least 3 matrix seeds; found N` |
| Internal-seed duplication | preflight | `preflight.py:548-562` | retryable | `candidate config and outer experiment matrix contain the same multi-seed list...` |
| Evaluator-owned literals not written by workload | preflight | `preflight.py:363-372` | retryable | `<file> references evaluator-owned output ...` |
| `run.py` serializes required metric keys | preflight | `preflight.py:191-314` | retryable | `<file> serializes metrics.json without required root key(s): ...` |
| Required artifacts statically referenced | preflight | `preflight.py:373-390` | retryable | `workload source does not statically reference required artifact: ...` |
| Contract digest unchanged | preflight/execution | `engine.py:5051-5052`, `5461-5462` | fail-closed | `evaluation contract changed after run creation` |
| Profile-vs-contract resource conflict | seal + matrix preflight | `engine.py:3567-3612`, `4956-4991` | **non-retryable** | `Contract candidate rejected by resource-authority preflight: ...` / `methodology.resource_authority_conflict` |
| Matrix resources exactly equal profile | preflight | `engine.py:5046-5050` | retryable | `imported benchmark resources must exactly match the preselected task resource profile` |
| Matrix runner image equals profile image | preflight | `engine.py:5041-5045` | retryable | `imported benchmark requires runner_image ...` |
| Matrix ≤ contract constraints | execution | `matrix.expand_experiment_matrix:47-55` | fail-closed | `experiment matrix exceeds evaluation-contract resource constraints` |
| Resources ≤ global policy | execution | `policy.py:140-156` | fail-closed | `requested resources exceed policy ceilings: ...` |
| Runner image in policy allowlist | execution | `policy.py` (classify) | fail-closed | (policy denial recorded on the action) |
| Contract candidate identity/references/schemas | seal | `contract_candidates.py:132-204` | retryable (agent redraft) | `candidate ... is invalid: ...` / `candidate references missing file: ...` |
| `methodology_requirements` shape | seal + promote | `contract_candidates.py:35-59`, `methodology_requirement_validation.py` | retryable | `methodology_requirements are invalid: ...` |
| Container image null on candidate | seal | `contract_candidates.py:154-160` | fail-closed | `shared-bundle candidates cannot choose a container image` |
| Contract digest / immutability | seal + resolve | `contracts.py:38-69`, `126-134`; `contract_candidates.py:255-273` | fail-closed | `contract digest mismatch: expected ..., got ...` |
| Promoted `id@version` immutability | promote | `contract_candidates.py:291-296` | fail-closed | `contract version is already promoted with another digest` |
| `budget.max_wallclock_minutes <= registry ceiling` | execution | workflow-api `job_submission.py:362-376` | fail-closed | `budget.max_wallclock_minutes exceeds the registry ceiling` |
| Workspace command argv | execution | workflow-api `schemas.py:1142-1153` | fail-closed | `workspace command ... is not approved` |
| Stored payload re-validation | read | `schemas.STORED_PAYLOAD_CONTEXT` `schemas.py:36`; `storage.py`/`postgres_store.py` `list_turns` | fail-closed (must not reject history) | generic 500 after #480 (`main.map_error` logs a traceback) |
| Evidence URIs resolve | preflight | `preflight.preflight_verification_evidence` | fail-closed | `evidence URI unresolved: <uri>` |

### 6.1 Rules enforced at the wrong layer

- **#480 - write-time rule applied on read.** The C6 `expected_sha256` rule is a
  *submission-time* rule, but `TurnRecord.model_validate` ran it again on every
  stored turn. Historical rows persisted before the rule became unreadable,
  breaking `GET /runs/{id}/turns` and every resume. The fix introduces
  `STORED_PAYLOAD_CONTEXT` (`schemas.py:36`) so write-time policy checks are
  skipped for stored payloads while structural rules still apply
  (`schemas.py:263-275`). Today the rule is [F3](#8-findings-and-defects)-fixed;
  the lesson is that a validator's context must distinguish "being written" from
  "being read".
- **#487 - a value the model cannot compute required at proposal time.** The
  digest of a remote URL is knowable only after fetching. Requiring it from the
  proposing model made every URL-declared task uncreatable. The fix moves
  population to an orchestrator preparer that fetches and hashes the bytes before
  validation (`engine.py:290-342`). Today fixed; the lesson is that
  model-uncomputable facts belong to deterministic code.
- **Metric keys on the generic path (NEW, open).** The rubric section is
  described as what "preflight enforces hardest"
  (`task-bundle-guide.md:73-75`), but for the generic contract
  `manifest.required_metric_keys` is `[]`, so
  `_source_errors`/`_metrics_root_errors` never checks the task's metric keys
  (`preflight.py:534-538`). They are enforced only by the evaluator after the job
  runs (`generic-task-integrity-v1/1.0.0/evaluator.py:43-46`). A job that omits a
  metric therefore burns cluster time before failing. See
  [F7](#8-findings-and-defects).
- **Profile-vs-contract on the binding path (NEW/known, #490).** The
  non-retryable classification exists on the candidate and matrix paths but not
  in `_promote_contract_candidate` (`engine.py:3925-3935`), producing an
  unresolvable resume loop. See [F6](#8-findings-and-defects).

---

## 7. Minimal viable `problem.md`

This example is constructed to satisfy every deterministic gate reachable from
source reading: one UTF-8 `problem.md`, a resolvable asset declaration, exact
metric keys, and evidence artifacts that the compiled source can statically
reference. **Firm vs inferred:**

- **Firm** (enforced by code read): file/count/UTF-8; the `glasslab-dataset://`
  URI shape (`schemas.py:237-240`); metric-key pattern
  (`schemas.py:289-291`); artifact path safety (`schemas.py:295-310`); that the
  runtime profile, image, command, resources, and workload are policy-selected
  and cannot be written here.
- **Inferred** (guide/prompt conventions only, no validator): the section
  headings, and the exact wording that steers the compiler toward a particular
  profile and asset list. The compile step is model-driven, so this document is
  *designed* to pass, not *proven* to pass without a live run.

Replace the digest with the one your `/dataset-upload` returned.

```markdown
# CIFAR-100 Seen/Unseen Representation Generalization

## Objective
Train a ResNet-18 representation on CIFAR-100 seen classes with
batch-hard triplet + supervised-contrastive losses, then measure
generalization to unseen classes.

## Inputs
- Dataset: `glasslab-dataset://0000000000000000000000000000000000000000000000000000000000000000`
  (replace with the sha256 returned by /dataset-upload; role: train,
  contains_labels: true)

## Method and architecture
- Backbone: ResNet-18 (exact); embedding dim 128.
- Losses: batch-hard triplet + supervised contrastive (SupCon).

## Hyperparameter search space (exact)
- Triplet margin: {0.1, 0.2, 0.3}; sampling: batch-hard.
- SupCon temperature: {0.07, 0.1}.
- Optimizer: AdamW, lr {1e-4, 3e-4}, weight decay 1e-4.
- Batch size: 128; epochs: 100.
- Early stopping: no validation-loss improvement for 10 epochs.

## Evaluation rubric (exact)
- Required metric keys (root of metrics.json):
  `test_seen_accuracy`, `test_unseen_accuracy`, `mean_recall_by_group`,
  `nmi`, `silhouette_score`.
- Pass thresholds: `test_unseen_accuracy >= 0.45` AND
  `mean_recall_by_group >= 0.5`.
- Stopping condition: stop after the approved matrix completes; at most one
  training run per seed.
- Effect-size claims require confidence level 0.95 via paired bootstrap
  (1000 resamples), reporting the interval. Seen vs unseen groups are compared
  with a paired test on the same seeds.

## Evidence artifacts (required)
- `metrics.json`, `report.md`, `plots/`, `tables/`, `source.zip`.
```

### What you can omit

- **The `eval_agent_prompt.md`** is optional; if absent the orchestrator writes a
  placeholder (`task_bundles.py:434-438`).
- **`assets` may be empty** if the task is synthetic, but then the workload has no
  data bindings and will likely fail at execution.
- **You never write** `display_name`, `runtime_profile`, `resources`,
  `workflow_id`, `command`, `source_subdirectory`, or any Kubernetes field; the
  compiler and policy produce those. The only place you can influence the profile
  is by describing the workload (prompt behavior: `engine.py:703-707`).
- **Artifact names beyond the required set** may be omitted; `metrics.json`,
  `report.md`, etc. are added by `BASE_REQUIRED_ARTIFACTS`
  (`task_bundles.py:140-150`). Do not list `evaluation.json` as something the
  workload must create - it is evaluator-owned (`preflight.py:72-78`).

### What you cannot omit

- The exact metric keys and the required evidence artifacts: without them the
  generic evaluator cannot score the run
  (`generic-task-integrity-v1/1.0.0/evaluator.py:43-52`).
- A resolvable dataset declaration, or the run will sit with `missing_inputs` and
  never pass task preflight.

---

## 8. Findings and defects

Each is annotated **already filed (#...)** or **NEW - needs an issue**. No issues
were created by this audit.

| # | Finding | Status | One-line justification (evidence) |
|---|---|---|---|
| F1 | `problem.md` has **no structural schema**; the guide's "mandatory sections" are unenforced conventions, and the parser-free design means a well-written but oddly-structured document can compile while a well-structured but vague one silently lands in `missing_inputs`. | **NEW - needs an issue** | `task_bundles.py:411-425` checks only filename/count; no markdown parser is a dependency; guide `task-bundle-guide.md:28-96` is prompt-only. |
| F2 | A value the model cannot compute (`expected_sha256` for a URL) was required at proposal time, making URL-declared tasks uncreatable (`POST /runs` 500). Now fixed by an orchestrator preparer. | already filed (#487) | `engine.py:297-342`; issue #487. |
| F3 | A write-time rule was enforced on read, making historical turns unreadable and breaking resume. Now fixed by `STORED_PAYLOAD_CONTEXT`. | already filed (#480) | `schemas.py:36`, `263-275`; issue #480. |
| F4 | Two independent resource ceilings made a requirement unsatisfiable; now converted to a non-retryable human pause, but the contract remains a second, independent ceiling (root cause only detected, not removed). | already filed (#483) | `matrix.py:47-55` vs `engine.py:5046-5050`; `engine.py:4956-4991`; issue #483. |
| F5 | `variants[].name` was stricter than sibling name fields; fixed to `^[a-z0-9][a-z0-9_-]{0,62}$`. | already filed (#474) | `schemas.py:425` vs `218/735/756/781`; issue #474. |
| F6 | The installed-contract binding path does not route the profile-vs-contract conflict to the non-retryable handler, causing an unresolvable 409 resume loop. | already filed (#490) | `engine.py:3925-3935` raises a bare `WorkflowError`; issue #490. |
| F7 | **Generic-path metric keys are not checked at preflight** even though the guide says the rubric is what preflight enforces hardest; they are enforced only by the evaluator after the job runs, wasting cluster time on a guaranteed failure. | **NEW - needs an issue** | `preflight.py:534-538` reads `contract.manifest.required_metric_keys` (empty for `generic-task-integrity-v1/1.0.0/contract.json`) while `evaluator.py:43-46` reads `task_spec.required_metric_keys`; guide `task-bundle-guide.md:73-75`. |
| F8 | **No cross-layer contract test** exists between the orchestrator's submission payload and workflow-api's `GenericExperimentRunRequest`; #491 (forbidden top-level `resources`, HTTP 422) survived because the fake executor never exercised the real schema. | already filed (#491) + **NEW - needs an issue for the test** | `cluster.py:227-299`; workflow-api `schemas.py:293-329`; issue #491 acceptance explicitly asks for such a test. |
| F9 | **The reason for fixed runtime profiles is not recorded anywhere** - no ADR/design doc/commit body explains why a task cannot determine its own resource envelope; only the security boundary is documented. This omission is what allowed #483's accidental exact-match semantics. | **NEW - needs an issue** | `docs/research-orchestrator.md:449-451`, `959`; commits `4aaeca5`/`54a5f58`/`8b2197f` have empty bodies. |
| F10 | **The contract's `manifest.budget` and `manifest.guardrails` are effectively inert**: the job's wall-clock comes from `spec.resources.wallclock_minutes`, not the contract budget, so a contract author's declared budget has no deterministic effect. | **NEW - needs an issue** | `schemas.py:475-487`; `cluster.py:279-281`; `job_submission.py:362-376` only checks `budget`, not `manifest.budget`. |
| F11 | **`variants[].name` drift guard is test-only, and the pattern is duplicated in three places** (`schemas.py:425`, prompt text `engine.py:161-162`, template sanitizer `engine.py:175-181`); a prompt change can diverge from the schema without any runtime failure. | **NEW - needs an issue** | `engine.py:155-159` notes the guard lives in `tests/test_matrix_template_derivation.py`, i.e. not enforced at runtime. |
| F12 | **The code default `permitted_job_images` allows only the CPU runner image**, so a GPU-profile task fails task preflight unless the deployment configmap overrides it (the live configmap does). A default deploy therefore cannot run GPU tasks. | **NEW - needs an issue** | `config.py:243-246` (one image) vs `10-configmap.yaml:88` (two images). |

---

## Appendix A: Authority map (single reference)

```text
problem.md (agent-read, no schema)
  -> TaskSpecProposal (glasslab-task-spec-v1)        [engine.py:701-718, schemas.py:279-310]
       -> TaskBundleRecord (compiled, policy-owned)  [task_bundles.py:476-582]
            resources = RUNTIME_PROFILES[profile]     [task_bundles.py:96-125, 571]
            required_artifacts = BASE + task          [task_bundles.py:140-150, 534-541]
  -> contract candidate -> seal -> promote -> resolve [contract_candidates.py, contracts.py]
  -> ExperimentMatrix (agent) -> matrix preflight     [preflight.py:441-581, engine.py:4993-5078]
       -> ExpandedJobSpec (deterministic)             [matrix.py:32-110]
            -> workflow-api GenericExperimentRunRequest [workflow-api schemas.py:293-329]
                 -> Kubernetes Job (registry resource_profile) [job_submission.py:602-608, 707-713]
```

## Appendix B: Evidence index

- Guides: `docs/research-orchestrator-task-bundle-guide.md`,
  `docs/research-orchestrator.md`, `docs/research-orchestrator-command-surface.md`.
- Orchestrator: `services/research-orchestrator/app/schemas.py`,
  `task_bundles.py`, `preflight.py`, `engine.py`, `matrix.py`,
  `contract_candidates.py`, `methodology_requirement_validation.py`,
  `contracts.py`, `cluster.py`, `policy.py`, `config.py`.
- Registry: `services/workflow-registry/definitions/workspace-cpu-ml-v1.json`,
  `workspace-gpu-ml-v1.json`.
- workflow-api: `services/workflow-api/app/schemas.py`, `job_submission.py`,
  `execution_routes.py`.
- Contracts: `services/research-orchestrator/evaluation-contracts/`.
- Issues: #467, #474, #480, #483, #487, #490, #491.
