# Workflow API

`workflow-api` is the v2 orchestration backend. It accepts structured requests, validates them against the approved workflow registry, creates canonical run manifests, stores run state, and hands execution to a bounded job submission interface.

Use this README with three buckets in mind:

- committed intent:
  - the API surface, schema expectations, and config defaults tracked in Git
- recently validated live behavior:
  - what has actually been checked from `.44` and documented in the live-state notes
- `.44` local only:
  - ignored secrets, current rollout image tags before push, and live cluster objects

Current architectural reality:

- investigations are the primary product aggregate
- sessions remain the working context used by intake, planning, and current
  command adapters
- skills/stages mutate session-owned state
- workflow families are increasingly execution templates, not the whole ontology
- mutating `latest` routes are still present for compatibility, but should not
  be treated as the durable primary contract for operator automation

What is committed here:

- API contracts
- store/backend selection behavior
- investigation, plan-approval, and evidence-link records
- session/skill route definitions
- execution-preflight and job-submission behavior
- source-document storage defaults
- build/source provenance fields and checks

What has been validated live recently:

- durable session metadata on the shared artifacts PVC, with Postgres now the
  committed live target
- session persistence across `workflow-api` pod restart
- execution preflight live against the current cluster
- bounded run submission through the approved workflow path

What still depends on `.44` local state:

- the currently deployed image tag before it is pushed and committed
- local secret manifests consumed by the deployment
- the exact live ConfigMap and Secret values in the cluster

Current provenance/debugging helpers:

- `/healthz` reports:
  - `build_source_revision`
  - `build_source_label`
- image build scripts now stamp build provenance into the container env and OCI labels:
  - `/home/gr66ss/cluster-config/scripts/push-workflow-api-image.sh`
  - `/home/gr66ss/cluster-config/scripts/build-import-workflow-api-image.sh`
- live deployment/image vs reported app provenance can be checked with:
  - `/home/gr66ss/cluster-config/scripts/check-workflow-api-provenance.sh`

Current durability warning:

- the store backend is now explicit:
  - `GLASSLAB_WORKFLOW_API_STORE_BACKEND=memory`
  - `GLASSLAB_WORKFLOW_API_STORE_BACKEND=json`
  - `GLASSLAB_WORKFLOW_API_STORE_BACKEND=postgres`
- in-memory mode is still valid for tests and short-lived local iteration
- `GLASSLAB_WORKFLOW_API_ALLOW_INMEMORY_STORE=false` now fails closed at settings load instead of silently booting on ephemeral state
- the committed live backend is Postgres via `GLASSLAB_WORKFLOW_API_STORE_POSTGRES_DSN`
- `GLASSLAB_WORKFLOW_API_STORE_JSON_PATH` is retained as migration/import input only
- artifact files and source-document blobs belong on the `.207` g-nas shared
  storage PVCs; Postgres stores references and summaries
- semantic/vector indexes live in Postgres through pgvector
- one-shot import helper for the existing JSON store:
  - `services/workflow-api/scripts/import-json-store-to-postgres.py`

Current execution prerequisites:

- dataset PVC: `glasslab-shared-datasets`
- artifacts PVC: `glasslab-shared-artifacts`
- image pull secret: `glasslab-ghcr-pull`
- service account RBAC able to read:
  - PVCs and secrets in `glasslab-v2`
  - cluster `nodes`
  - cluster `pods`

Current approved execution templates include coarse job shapes, not research-topic labels:

- `generic-tabular-benchmark` on `cpu-small`
- `literature-to-experiment` on `cpu-medium`
- `gpu-experiment` on `gpu-small`
- `replication-lite` remains declared-only until its submission path is implemented

Current GPU/CV execution contract:

- the workflow family is `gpu-experiment`
- the internal runner pipeline is `gpu_experiment`
- the intent is broader than any one ML subdomain:
  - computer vision
  - bounded GPU ML experiments
  - adjacent accelerator-backed model investigations
- execution preflight now surfaces declared `runtime_requirements` so the API can report:
  - required Python packages
  - supported modalities
  - dataset layout expectations

The repo now includes a direct prereq checker:

- `/home/gr66ss/cluster-config/scripts/check-v2-run-prereqs.sh`

The first live execution path now targets Kubernetes Jobs in `glasslab-v2` for accepted `generic-tabular-benchmark` runs.

Planned bounded stage-agent integration starts with the interpretation stage. The
first config surfaces now reserved are:

- `GLASSLAB_WORKFLOW_API_INTAKE_AGENT_ENABLED`
- `GLASSLAB_WORKFLOW_API_INTAKE_AGENT_URL`
- `GLASSLAB_WORKFLOW_API_INTAKE_AGENT_TIMEOUT_SECONDS`
- `GLASSLAB_WORKFLOW_API_INTERPRETATION_AGENT_ENABLED`
- `GLASSLAB_WORKFLOW_API_INTERPRETATION_AGENT_URL`
- `GLASSLAB_WORKFLOW_API_INTERPRETATION_AGENT_TIMEOUT_SECONDS`

Investigation aggregate endpoints:

- `POST /investigations`
- `GET /investigations/{investigation_id}/context`
- `POST /investigations/{investigation_id}/hypotheses`
- `POST /investigations/{investigation_id}/plans`
- `POST /investigations/{investigation_id}/plan-approvals`
- `POST /investigations/{investigation_id}/runs`
- `POST /investigations/{investigation_id}/claims`

Investigations are independent of compatibility research sessions. They
preserve the research question, exploratory or confirmatory mode, hypothesis
history, immutable execution graphs, approved plan hashes, stage-scoped runs,
and content-hashed evidence-backed claims. See
`docs/glasslab-v2/investigation-api-v1.md`.

Compatibility session-oriented operator endpoints:

- `POST /research-sessions`
- `GET /research-sessions/{session_id}/context`
- `POST /research-sessions/{session_id}/intake`
- `POST /research-sessions/{session_id}/transitions/prepare-current-plan`
- `GET /research-sessions/{session_id}/preflight/current-plan`
- `POST /research-sessions/{session_id}/transitions/run-happy-path`
- `GET /research-sessions/{session_id}/autoresearch-model-comparison`
- `POST /research-sessions/{session_id}/decisions/current`
- `POST /research-sessions/{session_id}/transitions/advance-autoresearch`

Aliases remain available under `/research-sessions/latest/...` while existing
callers migrate. They are not the investigation data model.

Supporting paper/source-intake endpoints:

- `POST /research-sessions`
- `GET /research-sessions`
- `GET /research-sessions/latest`
- `POST /research-sessions/bootstrap`
- `GET /research-sessions/latest/context`
- `POST /research-sessions/from-latest-research-problem`
- `POST /research-sessions/latest/research-problems/from-session-goal`
- `POST /research-sessions/latest/paper-intake-queues/from-latest-problem`
- `POST /research-sessions/latest/paper-intake-queues/stage-next-intake`
- `POST /research-sessions/latest/skills/external-literature-search`
- `POST /paper-intake-queues/from-research-problem`
- `GET /paper-intake-queues`
- `GET /paper-intake-queues/latest`
- `GET /paper-intake-queues/{queue_id}`
- `POST /paper-intake-queues/{queue_id}/stage-next-intake`
- `GET /source-documents`
- `GET /source-documents/latest`
- `GET /source-documents/{document_id}`
- `GET /interpretations/latest`
- `GET /replicability-assessments/latest`
- `GET /workflow-families/{workflow_id}/execution-preflight`

Session-first skill endpoints:

- `POST /research-sessions/{session_id}/skills/research-problem`
- `POST /research-sessions/{session_id}/skills/literature-harvest`
- `POST /research-sessions/{session_id}/skills/external-literature-search`
- `POST /research-sessions/{session_id}/skills/paper-intake`
- `POST /research-sessions/{session_id}/skills/interpretation`
- `POST /research-sessions/{session_id}/skills/assessment`
- `POST /research-sessions/{session_id}/skills/design`
- `POST /research-sessions/latest/skills/research-problem`
- `POST /research-sessions/latest/skills/literature-harvest`
- `POST /research-sessions/latest/skills/external-literature-search`
- `POST /research-sessions/latest/skills/paper-intake`
- `POST /research-sessions/latest/skills/interpretation`
- `POST /research-sessions/latest/skills/assessment`
- `POST /research-sessions/latest/skills/design`

Autoresearch endpoints:

- `POST /autoresearch/campaigns`
- `GET /autoresearch/campaigns/latest`
- `GET /autoresearch/campaigns/{campaign_id}`
- `POST /autoresearch/campaigns/{campaign_id}/draft-initial-methodologies`
- `POST /autoresearch/campaigns/{campaign_id}/launch-next-iteration`
- `GET /autoresearch/campaigns/{campaign_id}/iterations`
- `POST /autoresearch/campaigns/{campaign_id}/decide-latest`
- `GET /autoresearch/campaigns/{campaign_id}/summary`

Session-scoped autoresearch transitions:

- `POST /research-sessions/{session_id}/transitions/start-autoresearch-campaign`
- `POST /research-sessions/{session_id}/transitions/draft-methodologies`
- `POST /research-sessions/{session_id}/transitions/launch-autoresearch-iteration`
- `POST /research-sessions/{session_id}/transitions/decide-autoresearch-latest`
- `POST /research-sessions/{session_id}/transitions/draft-autoresearch-notebook`
- `GET /research-sessions/{session_id}/autoresearch-summary`
- matching `latest` aliases under `/research-sessions/latest/...`

Notebook drafting:

- `POST /autoresearch/campaigns/{campaign_id}/draft-analysis-notebook`

This writes a deterministic `analysis_notebook.ipynb` scaffold under the shared artifacts path for the selected methodology draft. The first pass is backend-owned and template-driven, not free-form model notebook generation.

The first pass is intentionally narrow:

- campaign creation is design-backed and session-owned
- methodology drafts are structured records, not free-form model plans
- run launch still passes through the approved workflow registry path
- automatic decisions stay conservative and escalate when evidence is weak

These are thin bounded aliases over the existing session-state path. The intent is:

- sessions hold the research state
- skills mutate that state in bounded ways
- workflow families stay a later execution concern
- design prefers the session's latest ready assessment when available, then falls
  back to the latest intake path

Session-scoped read endpoints:

- `GET /research-sessions/{session_id}/context`
- `GET /research-sessions/{session_id}/research-problem`
- `GET /research-sessions/{session_id}/paper-intake-queue`
- `GET /research-sessions/{session_id}/source-document`
- `GET /research-sessions/{session_id}/intake`
- `GET /research-sessions/{session_id}/interpretation`
- `GET /research-sessions/{session_id}/assessment`
- `GET /research-sessions/{session_id}/design`
- matching `latest` aliases under `/research-sessions/latest/...`

Use these as the primary read contract when OpenClaw or an operator is working
inside a session. The older global `latest` stage endpoints remain available for
compatibility, but they are no longer the preferred session-oriented surface.

Session memory endpoints:

- `POST /research-sessions/{session_id}/notes`
- `POST /research-sessions/latest/memory`

These let the active research session retain:

- working notes from the conversation
- concrete decisions about what is worth investigating
- bounded next experiment ideas that should persist beyond one chat turn

Operation-record endpoints:

- `GET /operations`
- `GET /operations/latest`
- `GET /operations/{operation_id}`

The first covered operation types are:

- `literature-harvest`
- `source-document-fetch`
- `paper-intake`

These let `workflow-api` persist a bounded queue of harvested paper candidates
before interpretation/assessment/design work begins. The queue is intended to
run in the background while later paper-understanding work is still being
refined.

Paper-intake queues now also preserve harvester coverage metadata, so the
session can distinguish between:

- strong corpus matches
- thin lexical matches
- fallback shortlist selection from the approved seed corpus
- filtered results that were narrowed by policy or priority

There are now two queue-generation paths:

- `literature-harvest`: bounded approved seed-manifest selection
- `external-literature-search`: external metadata search that writes durable candidates back into the same session queue structure

The newer session layer makes that state conversationally usable:

- a `ResearchSessionRecord` is now the stateful literature workspace
- sessions track the latest problem, queue, document, intake, interpretation,
  assessment, design, and run
- workflow families stay execution-oriented
- sessions are the stateful "how we think" layer for back-and-forth literature work

When a queued paper is staged, `workflow-api` now fetches the paper URL and
creates a `SourceDocumentRecord`. Storage is explicit:

- filesystem mode: writes under `GLASSLAB_WORKFLOW_API_SOURCE_DOCUMENT_STORAGE_DIR`
- MinIO mode: writes to `GLASSLAB_WORKFLOW_API_SOURCE_DOCUMENT_BUCKET` using the
  bucket-scoped identity in the `glasslab-v2-workflow-api-minio` Secret
  (`MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY`), never the MinIO root credential; see
  `docs/glasslab-v2/runbooks/provision-minio-scoped-users.md`

By default, filesystem storage writes paper/source blobs under:

- `/mnt/artifacts/source-documents`

Interpretation now carries forward:

- `literature_state_summary`
- `research_gaps`
- `bounded_experiment_ideas`

and those outputs now inform later assessment and design stages instead of
stopping at the interpretation boundary.

Execution is also more explicit now:

- `workflow-api` exposes an execution-preflight result per workflow family
- run acceptance now checks that preflight before submission
- Kubernetes job submission now applies the registry-declared resource requests,
  limits, and node selector to the actual runner pod spec
- workflow registry entries now declare:
  - `execution_status`
  - `submission_backend`
  - `execution_blockers`
- this lets preflight report "declared but not executable" instead of over-promising from the registry alone
