# workflow-api Route Ownership

Status: current as of the T7 ownership cut (2026-09-04).

The workflow-api exposes a broad HTTP surface. This document records which
routes are owned by which consumer so that route removal is a deliberate,
reviewed decision rather than an accidental break.

## Orchestrator-owned routes (exactly 4)

The research-orchestrator (`services/research-orchestrator/app/cluster.py`)
is the only consumer of these routes. It calls exactly these four and nothing
else; `test_cluster.py::test_cluster_uses_exactly_the_owned_workflow_api_endpoints`
locks that set.

| Method | Path | Owner | Purpose |
|---|---|---|---|
| POST | `/experiments/runs` | research-orchestrator | Submit a bounded experiment run |
| GET | `/runs/{id}` | research-orchestrator | Inspect run status |
| GET | `/runs/{id}/artifacts` | research-orchestrator | Fetch run artifacts for evidence delivery |
| POST | `/runs/{id}/cancel` | research-orchestrator | Cancel a bounded run |

## Retained surfaces

These route families remain registered and are owned by the named consumers.

| Surface | Owner | Notes |
|---|---|---|
| Investigations (`/investigations*`) | research-orchestrator / research front door | `register_investigation_routes` |
| Literature (`/research-sessions*`, `/research-problems*`, `/paper-pipelines*`) | research-orchestrator / research front door | `register_literature_routes` |
| Execution (`/experiments/runs`, `/runs`, `/workflow-families`, preflight) | research-orchestrator | `register_execution_routes` |
| Schedule (`/digest-schedules*`) | research-orchestrator | `register_schedule_routes` |
| Source documents, technique catalog | research-orchestrator | `register_source_document_routes`, `register_technique_catalog_routes` |

## Cut surfaces (deregistered)

These route families were removed from `create_app` in the T7 ownership cut.
Their modules remain in the tree for reference but are not registered.

| Surface | Reason |
|---|---|
| Autoresearch (`/autoresearch/campaigns*`, autoresearch session transitions) | Not used by the orchestrator; superseded by the bounded research path |
| Stage agents (`/interpretations/from-latest-intake`, `/design-drafts/from-latest-intake`, `/replicability-assessments/from-latest-interpretation`, `/research-sessions/{id}/skills/design`, stage transitions) | Not used by the orchestrator |

## Contract

- Any new route consumed by the orchestrator must be added to the
  orchestrator-owned table above and to the locking test.
- Any route removed from `create_app` must be recorded here before removal.
- The orchestrator must never call a workflow-api route outside the four
  owned endpoints; the locking test fails if it does.