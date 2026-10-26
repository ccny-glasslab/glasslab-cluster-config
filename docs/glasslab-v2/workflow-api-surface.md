# `workflow-api` Surface Ownership

Status: **decision recorded 2026-10-26** (issue #352, hyperplan T7/E2).

**Decision:** `workflow-api` is the bounded cluster-execution control plane. The
**autoresearch second-automation surface and the stage agents are cut** (their
modules remain but are not registered; `test_owned_routes.py` asserts 404). The
surface that remains is **explicitly owned** per the table below. Two retained
modules have no in-repo consumer today — they are owned by the workflow-api
product surface and are candidates for a follow-up cut, not silent drift.

## Method

- Enumerated the route modules under `services/workflow-api/app/`.
- Confirmed which `register_*_routes` calls `create_app` actually makes.
- Traced the two real clients:
  - `services/research-orchestrator/app/cluster.py`
    (`WorkflowApiClusterExecutor`),
  - `services/schedule-worker/app/main.py`.
- Checked the enforced contract in
  `services/workflow-api/tests/test_owned_routes.py`.

`main.py` registers: `literature`, `source_documents`, `technique_catalog`,
`execution`, `investigation`, `schedule`. It does **not** register
`autoresearch_routes`, and the stage-agent paths 404.

## Live contract (individually owned)

| Endpoint | Method | Consumer | Owner |
|---|---|---|---|
| `/experiments/runs` | POST | research-orchestrator cluster executor (submit) | research-orchestrator control plane |
| `/runs/{run_id}` | GET | research-orchestrator (status) | research-orchestrator control plane |
| `/runs/{run_id}/artifacts` | GET | research-orchestrator (evidence artifacts) | research-orchestrator control plane |
| `/runs/{run_id}/cancel` | POST | research-orchestrator (bounded cancel) | research-orchestrator control plane |
| `/digest-schedules/run-due` | POST | schedule-worker (per-invocation) | schedule-worker |
| `/approved-rerun-schedules/run-due` | POST | schedule-worker (per-invocation) | schedule-worker |

The orchestrator treats a 404 on `/runs/{id}/cancel` as a hard error ("configured
workflow-api does not expose bounded cancellation"), so cancel is required.

## Retained product surface (module-level owner)

These are registered and therefore part of the tested contract. They have no
in-repo caller except where noted; the owner is the workflow-api product surface
unless a consumer is named.

| Module | Routes (approx.) | Status / owner |
|---|---|---|
| `execution_routes.py` (registry, `/workflow-families`, preflight, `/research-sessions`, `/runs/from-*`, `/experiments/compare`) | 16 | retained; owner: workflow-api product surface (the 4 orchestrator endpoints above are the live contract) |
| `investigation_routes.py` | 10 | retained; **no in-repo consumer** — owner decision pending |
| `literature_routes.py` | 49 | retained; **no in-repo consumer** — owner decision pending |
| `schedule_routes.py` (CRUD + run-due) | 9 | retained; `run-due` owned by schedule-worker, CRUD by the product surface |
| `source_documents.py` | 3 | retained; owner: workflow-api product surface |
| `technique_catalog.py` | 3 | retained; owner: workflow-api product surface |

## Already cut (do not re-register without a new owner decision)

| Surface | Evidence |
|---|---|
| autoresearch campaigns (`autoresearch_routes.py`) | module exists, **not registered**; `test_owned_routes.py` asserts 404 |
| autoresearch session transitions | asserted 404 |
| stage agents (`interpretations/*`, `replicability-assessments/*`, `design-drafts/*`, `research-sessions/*/skills/design`) | asserted 404 |
| transition stage routes (`/transitions/create-*`) | asserted 404 |

## Follow-up decision for the unconsumed retained modules

`investigation_routes.py` and `literature_routes.py` are registered but nothing
in this repo calls them. Pick one, explicitly:

1. **Own it** — name a consumer/owner (e.g. a future front door) and add it to the
   owners table. No code change.
2. **Cut it** — remove the `register_*` call and the module, and update
   `test_owned_routes.py`'s "retained investigations/literature exist" assertions
   in the same PR.

Until either is recorded, they are "retained, unconsumed" — not a second product
surface with an implicit owner.

## Guardrails

- The six live-contract endpoints must stay green; the orchestrator's
  `ClusterExecutor` and the schedule-worker models depend on them.
- Removing a retained module is a normal release (`testing` → `main` → rollout),
  not an in-place edit.
- Keep `test_owned_routes.py` in lockstep with any surface change.

## References

- Issue #352 (this decision), #491 (budget/resource authority), #602
  (service-to-service auth).
- Clients: `services/research-orchestrator/app/cluster.py`,
  `services/schedule-worker/app/main.py`.
- Tests: `services/workflow-api/tests/test_owned_routes.py`,
  `test_schedule_execution.py`, `test_api.py`.
