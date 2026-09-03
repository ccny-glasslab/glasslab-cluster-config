# Research Investigations: Design

The organizing entity for Glasslab research. Supersedes
`research-project-design.md` (2026-09-02, withdrawn): the "Project" entity was
a competing second root. The repository already declares **Investigation** the
product-level research record; this document aligns the orchestrator with it.

Status: **design** (2026-09-03). The orchestrator's Project entity is being
removed; runs and conversations re-scope to `investigation_id`.

## 1. The root is Investigation — and it already exists

`docs/glasslab-v2/investigation-api-v1.md`: "An investigation is Glasslab's
product-level research record." It owns hypotheses, immutable plan revisions,
plan approvals frozen by SHA-256, linked runs, evidence, and claims. The
README says the same: "one canonical investigation record."

The orchestrator does **not** own investigations. It owns bounded execution.
Its runs and conversations must therefore **attach to an investigation by
`investigation_id`** (a foreign reference), not wrap the investigation in a
competing aggregate.

```
Investigation (workflow-api: hypotheses, plan revisions, approvals, claims)
  └─ orchestrator scoping: runs + conversations keyed by investigation_id
       ├─ conversations and promotions (frozen snapshot digests)
       ├─ bounded runs (per-investigation limit + global admission ceiling)
       ├─ verified artifacts and claims
       └─ immutable publication revisions
```

No `Project` entity. No second root. The orchestrator's `ProjectRecord` is
removed; its useful mechanics (per-scope run slots) survive keyed by
`investigation_id`.

## 2. Principles

1. **Investigation is the unit of research.** One research question, with
   hypotheses, plan revisions, a campaign of bounded runs, and an
   accumulating published record. Everything else attaches.
2. **The orchestrator scopes; workflow-api owns.** The orchestrator never
   creates or mutates investigations; it references `investigation_id` on
   runs and conversations and enforces execution policy per investigation.
3. **Plans are immutable revisions.** A run freezes the exact plan revision
   (schema version, digest, source commit, revision id) it was approved
   against. Revisions can never silently change scientific scope.
4. **Deterministic code owns execution state.** Approvals, gates, admission,
   publishing, and transitions stay deterministic, exactly as with runs.
   Agents propose; the orchestrator decides; evidence is immutable.
5. **Humans get projections, not surgery.** Researchers use the agentic
   surface and Discord; stakeholders get a stable URL with progress and
   finished reports.

## 3. Orchestrator scoping by investigation_id

`RunRecord.investigation_id: str | None` (renamed from `project_id`). The
same on conversation runs. The id is a foreign reference — the orchestrator
does not validate investigation existence at run creation (workflow-api owns
the lifecycle); it is recorded for scoping, reporting, and policy.

No `investigation_id` on a run = legacy un-scoped execution (the current
global slot semantics).

## 4. Concurrency: per-investigation limit AND a global ceiling

The current single-active-run policy was global. The corrected design keeps
**both** bounds:

- **Per-investigation limit** (default 1): campaign coherence — one bounded
  run at a time within an investigation. Enforced atomically at run
  creation: `state NOT IN (terminal) AND conversation = false AND
  investigation_id = ?`.
- **Global admission ceiling**: a cluster-safety cap on total concurrent
  orchestrator runs across all investigations. Enforced atomically with the
  per-investigation check; operator overrides are audited events.

Un-scoped runs (no `investigation_id`) share the legacy global slot and count
against the global ceiling.

## 5. Conversations and promotions (durable relation)

A conversation attaches to an investigation via `investigation_id` on the
conversation run. Promotion becomes a **durable relation**, not a
one-shot event:

- Each promotion records: `investigation_id`, the frozen **conversation
  snapshot digest** (hash of the turns + bound sources at promotion time),
  the created `run_id`, the objective, and the plan revision the run freezes.
- **Multiple promotions are allowed and explicitly identified**: a
  conversation may seed several campaign runs within its investigation; each
  promotion is a distinct record. The old
  "one promotion per conversation" guard is removed.
- The promoted run inherits the investigation's default contract and dataset
  bindings, and its seed context comes from the frozen snapshot (so the
  protocol draft reflects exactly what the operator approved, even if the
  conversation keeps evolving).

## 6. Datasets: catalog references by id/digest

- Operators register a dataset once into the **catalog** (upload or
  URL-import with sha256 verification; the task-asset-downloader work,
  #233).
- Catalog records use **dataset ids and content digests as identities** —
  never bare names (names are not unique/safe).
- An investigation's dataset bindings reference catalog ids. `role`
  (training/validation/etc.) is **binding-specific**, declared per binding,
  not an intrinsic property of the stored bytes.
- Runs materialize catalog datasets by id into scratch (the existing
  `_materialize_objective_datasets` generalizes from objective URIs to
  investigation + run dataset refs).

## 7. Reports and publishing (a real publisher)

The report package is **not** a pre-existing artifact to copy. Delivery
dynamically selects and verifies records across run-level and job artifacts.
The publisher therefore builds a formal, **immutable package manifest**:

- Package contents: report.md, plots/, tables/, metrics.json,
  evaluation.json, source (when required), checksums per artifact.
- Inclusion rules: matrix-job selection, failed-job evidence, superseded
  artifacts, completeness requirements — declared and verified, not
  best-effort.
- The manifest is content-addressed and frozen at publication.

The publish target is MinIO (`s3://artifacts/projects/<investigation-slug>/`
layout) — **with the wiring actually built**: bucket provisioning, endpoint +
credentials (already available in the deployment secrets, wired explicitly),
access policy, immutable object naming, atomic index updates, retries, and
failure recovery. No "no new credentials needed" assumptions: the publisher
is a designed component with its own tests.

A stable stakeholder URL requires an explicit access/authorization decision
(presigned links vs. an authenticated index) — left as an open question,
not assumed.

## 8. Filesystem: references, never duplicates

No copying of datasets, run trees, or reports into an aggregate directory.
No symlinks (conflicts with artifact-integrity rules).

Projections on the shared mount contain **indexes/references only**:
- `investigations/<investigation_id>.json` — the investigation's catalog
  dataset ids, run ids, promotion records, and publication digests
- Content-addressed artifacts stay in their canonical store locations

Storage identity is the `investigation_id` (or artifact digest), never a
mutable slug.

## 9. Surfaces

### 9.1 REST API (canonical, operator-gated)

- Orchestrator: runs and conversations accept `investigation_id`; run list
  filters by investigation; per-investigation concurrency reports.
- workflow-api: the Investigation lifecycle (create, hypotheses, plan
  revisions, approvals) is unchanged and authoritative.

### 9.2 Discord

Run status/approvals show their investigation. `/research-question` threads
attach to an investigation; promote creates a run within it. No new
command surface for investigations (workflow-api owns that lifecycle).

### 9.3 Agentic surface

Conversations are investigation-scoped; `promote_conversation` takes
`investigation_id` and records the frozen snapshot relation.

### 9.4 Published output

Stakeholders get one stable URL per investigation (MinIO index), showing
progress + finished report packages. The URL/access model is the open
question in §7.

## 10. Lifecycle

```
Investigation (workflow-api): hypotheses -> plan revisions -> approvals
  └─ orchestrator: runs scoped by investigation_id
       conversation -> promotion (frozen snapshot) -> run -> gates -> report
       -> publication revision -> index update -> stakeholder URL
```

The run loop is unchanged: approvals, deterministic job submission,
immutable evidence, evaluator authority.

## 11. Migration from the shipped Project entity

1. Rename `project_id` -> `investigation_id` on runs and conversations
   (schemas, both stores' runs column + predicate, engine, API).
2. Remove `ProjectRecord`, `ProjectStatus`, `ProjectCreateRequest`, the
   `projects` tables, the `/projects` API endpoints, and
   `engine.create_project` / `archive_project`.
3. Replace the one-promotion guard with the durable promotion relation
   (§5) — the conversation keeps a `conversation.promotions` record.
4. Keep the per-scope slot mechanics, re-keyed to `investigation_id`,
   and add the global admission ceiling (§4).
5. The catalog (§6) and publisher (§7) land as their own slices; the other
   agent's catalog work feeds §6.

Each step is additive where possible; the Project entity is removed, not
fenced.

## 12. Boundaries (unchanged)

- Deterministic code owns execution state, admission, publishing, and
  transitions.
- Agents propose; humans approve at the same gates; job submission stays
  bounded and policy-checked; evidence stays immutable.
- The publisher copies already-accepted, already-immutable artifacts — it
  grants no new authority. MinIO credentials are wired explicitly with a
  secret review.

## 13. Open questions

- **Global admission ceiling value**: what cap (e.g., 3 concurrent runs
  across investigations) matches cluster capacity? Revisit after the first
  real campaign.
- **Stakeholder URL model**: presigned links vs. authenticated index for
  report access.
- **Investigation id validation**: should the orchestrator verify the id
  exists in workflow-api before accepting it on a run, or treat it as an
  opaque foreign key (proposed: opaque; workflow-api is the validator).
- **Promotion snapshot scope**: whether a promotion's frozen snapshot should
  exclude later conversation turns (proposed: yes — the snapshot is the
  contract).