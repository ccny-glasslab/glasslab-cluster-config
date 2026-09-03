# Research Projects: Design

The organizing entity for Glasslab research, replacing the fragmentary
tracking mechanisms accumulated along the way.

Status: **design** (2026-09-02). Not yet implemented.

## 1. Problem: herding sheep without a dog

Research work is currently tracked by several overlapping mechanisms, each
invented for a different moment and none of them an organizing whole:

| Mechanism | Where | What it tracks |
|---|---|---|
| Conversation runs | `engine.answer_research_question` | inert runs (`conversation=True`) holding chat turns + bound sources |
| `conversation.promoted` events | `engine.promote_conversation` | one-shot link from a conversation to a run |
| Task bundles | `services/research-orchestrator` task-bundle store | a problem statement + task spec + dataset assets + contract |
| Dataset uploads | `dataset-uploads/` + `glasslab-dataset://<sha>` URIs | raw data, referenced from objectives |
| Benchmark dataset catalog | `benchmark_dataset_catalog_path` + `IngestedDatasetRecord` | the seed of a catalog, unfinished |
| Per-run reports | `runs/<run_id>/reports/` on the NFS mount | final report, effectively invisible |
| Discord threads | `discord_thread_id` on runs | the run's operator-facing home |
| Single-active-run policy | `create_run(one_active_run=…)` | one bounded execution at a time |

The result: a campaign (a research question pursued over many runs, datasets,
conversations, and reports) has **no first-class identity**. It is implicit
in whichever fragment you happen to be looking at. This document introduces
the missing entity — the **Project** — and specifies how every existing
mechanism attaches to it.

## 2. Principles

1. **The Project is the unit of research.** A research question, pursued
   over conversations, datasets, and a campaign of bounded runs, with an
   accumulating published report log. Everything else is a child or a
   projection.
2. **Projects are declarative (as code) and durable (in the store).** The
   declaration lives in git as a project manifest (reviewable, reproducible,
   importable); the runtime truth lives in the orchestrator store. The
   manifest and the store record must not drift: the manifest is the seed,
   the store is authoritative for runtime state.
3. **Deterministic code owns project state.** Approvals, gates, publishing,
   and state transitions stay deterministic, exactly as with runs. Agents
   propose; the orchestrator decides; evidence is immutable.
4. **Humans get projections, not surgery.** Researchers interact through the
   agentic surface and Discord; stakeholders get a stable URL with progress
   and finished reports. Nobody pokes the store.
5. **One way to bind data, one way to publish, one way to delineate.** The
   dataset catalog, the report publisher, and the Project entity each replace
   a cluster of ad-hoc mechanisms.

## 3. The Project entity

### 3.1 Schema (additive to both stores, JSON-payload pattern)

```
ProjectRecord:
  project_id: str          # uuid4().hex, stable
  slug: str                # kebab-case, url-safe, unique per manifest
  title: str               # human title
  objective: str           # the research question (the reason it exists)
  status: ProjectStatus    # ACTIVE | ARCHIVED
  dataset_ids: list[str]   # catalog dataset references (not copies)
  default_contract_id/version/digest
  created_by: str          # operator identity
  created_at, updated_at
  report_count: int        # accumulated published reports
```

States: `ACTIVE` (can spawn runs, bind datasets, attach conversations),
`ARCHIVED` (read-only; runs remain immutable; publishing stops).

Store tables `projects` (SQLite + Postgres) follow the established
JSON-payload record pattern with a `project_id` real column for indexes.

### 3.2 The project manifest (as code)

A project is declared in git:

```text
projects/<slug>/project.yaml
  title, objective
  datasets: [{name, uri-or-catalog-ref, sha256, role, contains_labels}]
  default_contract: {id, version}
  created_by
```

- Imported via a bounded operator action (`/project-start` or API), like task
  bundles today: checksum-verified, installed, then materialized into the
  store as a `ProjectRecord` in `ACTIVE`.
- The manifest is the *declaration*; the store record is *runtime truth*.
  Re-importing the same manifest must be idempotent (same `slug` → same
  `project_id`, updates metadata only, never destroys state).

## 4. Surfaces: how we interact with projects

### 4.1 REST API (operator-token gated, canonical)

- `POST /projects` — import a manifest (body: manifest text or bundle ref)
- `GET /projects` / `GET /projects/{id}` — list / view (children included:
  datasets, runs, reports, conversations)
- `POST /projects/{id}/datasets` — bind catalog datasets
- `POST /projects/{id}/archive` — terminal (read-only) state
- `GET /projects/{id}/reports` — the published report log
- `GET /projects/{id}/runs` — the campaign's runs

### 4.2 Discord (operator + stakeholder projection)

- `/project-start` — declare a project from a manifest (or from a thread:
  the thread becomes the project's home, reusing the `discord_thread_id`
  mechanism)
- `/project-status` — the project's state: datasets, recent runs, report
  log — rendered from the same `build_run_status_view`-style projection
- Run approvals and status stay exactly as today, but every run is now
  project-scoped, so a run's status message shows its project
- Existing `/research-question` threads *belong to* a project once one is
  created; promoting a conversation promotes *within* the project

### 4.3 Agentic surface (OpenCode-backed chat)

Conversations are the researcher's working surface; the Project is the
container. A conversation attaches to a project (by thread, by
`project_id`, or by starting a new project). `promote_conversation` gains a
project-scoped variant: the promoted run records `project_id`, its seed
context and bound sources come from the conversation as today, and the
protocol draft references the project's catalog datasets.

### 4.4 Filesystem (canonical layout on the shared mount)

```text
/mnt/artifacts/research-orchestrator/
  projects/<slug>/
    project.yaml            # the imported manifest (copy)
    datasets/               # materialized catalog dataset copies
    runs/                   # campaign runs (run dirs, symlinked or copied)
    reports/                # accumulated published report packages
```

One visible place per project. Nothing in the run/artifact database ever
references scratch paths; the same rule applies here.

### 4.5 Published output (stakeholder surface)

After a run's report is accepted, a deterministic publish step writes to
MinIO (`s3://artifacts`):

```text
s3://artifacts/projects/<slug>/index.json           # project summary
s3://artifacts/projects/<slug>/runs/<run_id>/       # report.md, plots/,
                                                    # tables/, metrics.json,
                                                    # evaluation.json, source.zip
s3://artifacts/projects/<slug>/index.html           # generated static index
```

A static index page means a stakeholder gets one stable URL per project and
sees progress + finished reports without touching the orchestrator, Discord,
or a terminal. (Git-repo publishing is a later, optional variant needing a
deploy credential; MinIO needs none.)

## 5. Children: how the fragments attach

### 5.1 Conversations

A conversation (research-chat thread, `conversation=True` run) gains an
optional `project_id`. It keeps its turns, bound sources, and
`conversation.promoted` linkage — but now it is *inside* a project. The
one-promotion-per-conversation rule becomes one-promotion-per-conversation-
*within-a-project* (a conversation can seed multiple runs across a campaign,
which the project now makes coherent).

### 5.2 Runs

`RunRecord` gains `project_id: str | None`. Runs created from a project
manifest, by promote, or by `/research-start` inside a project thread are
project-scoped. The single-active-run policy becomes **one active run per
project** (the global slot relaxes to a per-project slot; the default
concurrency for a project is still 1, operator-overridable). All gates,
immutability, and evidence semantics are unchanged.

### 5.3 Datasets: the catalog

The dataset intake generalizes from case-by-case uploads to a **catalog**:

- Operators register a dataset once: upload, or **import from URL with
  sha256 verification** (the #233 task-asset-downloader work, revived).
- The catalog record: `name` (stable), `uri` (s3:// or dataset://),
  `sha256`, `role` (training/validation/etc.), `contains_labels`,
  `provenance`.
- Projects reference catalog datasets by name; the engine materializes them
  into the project's `datasets/` when a run needs them (the existing
  `_materialize_objective_datasets` generalizes from objective URIs to
  project + run dataset refs).
- Titanic, wine, fashion-mnist, adult-income become catalog entries, not
  bespoke bundles.

### 5.4 Reports: the publish step

The final report no longer dies on the NFS mount:

- On **report acceptance** (the final human gate, unchanged), a deterministic
  publisher copies the report package to the project's MinIO path and
  regenerates the project index.
- The report package is already complete today (report.md, plots/, tables/,
  metrics.json, evaluation.json, source.zip) — publishing is a copy + index,
  no new authoring.
- Discord gets a link (`/project-status` and the acceptance message show the
  URL), so the human loop stays where it is and Mike gets the link.

## 6. Lifecycle

```
project-start (manifest or thread)
  -> ACTIVE
  -> bind catalog datasets            (POST /projects/{id}/datasets)
  -> conversations attach             (research threads / chat)
  -> campaign: run loop               (protocol -> approval -> jobs ->
                                       evaluator -> report -> acceptance)
     each run: project-scoped, one active per project
  -> report acceptance -> publish      (MinIO + index + URL)
  -> project-archive                  (read-only; runs immutable)
```

Nothing about the run loop itself changes: approvals, deterministic job
submission, immutable evidence, and evaluator authority are untouched. The
Project wraps the loop with identity, data, and a visible output.

## 7. Migration from the fragmentary state

1. **Project entity + store + manifest import** (the dog exists).
2. **Catalog intake** (register-once; revive #233's URL+checksum import).
3. **Project-scope runs + conversations** (`project_id` fields; promote
   becomes project-scoped).
4. **Publisher** (report acceptance -> MinIO + index + URL).
5. **Discord surfaces** (`/project-start`, `/project-status`).
6. Backfill: existing conversations become ad-hoc projects (slug from the
   thread id); existing runs attach to their project by promote linkage.

Each step is additive; the fragmentary mechanisms are retired by
*attachment*, not deletion (nothing is removed until its replacement is
proven).

## 8. Boundaries (unchanged)

- Deterministic code owns project state, publishing, and transitions.
- Agents propose; humans approve at the same gates; job submission stays
  bounded and policy-checked; evidence stays immutable.
- The publish step is a copy of already-accepted, already-immutable
  artifacts — it grants no new authority.
- No new credentials for MinIO publishing (MinIO API keys are already in the
  deployment); git-repo publishing (if adopted later) requires a separate
  secret review.

## 9. Open questions

- **Project concurrency**: one active run per project vs a global cap.
  Default per-project 1; revisit after the first real campaign.
- **Manifest authority**: is the manifest the single source for a project's
  dataset bindings, or can operators bind ad hoc via API after creation?
  (Proposed: manifest binds at creation; API adds later; store wins on
  conflict.)
- **Delineation granularity**: one project per research question, per
  investigator, or per stakeholder deliverable? (Proposed: per research
  question; investigators can group by sharing the question.)
- **Report retention**: per-project retention vs the current per-run
  terminal cleanup. (Proposed: published reports are never cleaned by
  storage retention; they are the durable product.)