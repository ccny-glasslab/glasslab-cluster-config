# Glasslab System Map 2026-07

Status: current simplification map

Date: 2026-07-23

This file answers:

> What exists, what is current, and what should be simplified next?

Glasslab has several overlapping generations of infrastructure. The cleanup
rule is to keep one primary path for learning tasks and make every other path
explicitly secondary, compatibility-only, or historical.

## Current Primary Shape

The primary operator path is:

```text
OpenCode
  -> exo OpenAI-compatible model on the Mac pair
  -> repo-owned scripts
  -> workflow-api
  -> Kubernetes Job
  -> artifacts + metrics
  -> compare / decide / next
```

The primary control plane is:

```text
services/workflow-api
```

The primary product aggregate is:

```text
investigation
  -> hypotheses
  -> immutable execution-graph plans
  -> approved plan snapshot
  -> runs
  -> evidence-backed claims
```

The first current API contract for that aggregate is:

```text
docs/glasslab-v2/investigation-api-v1.md
```

The primary registry is:

```text
services/workflow-registry
```

The primary record store is:

```text
Postgres in namespace glasslab-v2
```

The primary learning-task contract is:

```text
POST /investigations/{investigation_id}/plans
POST /investigations/{investigation_id}/plan-approvals
POST /investigations/{investigation_id}/runs
workload_id = research-workspace-cpu-v1 or another registry-backed workload
```

## Source Of Truth By Concern

| Concern | Current home | Notes |
| --- | --- | --- |
| Physical lab, PXE, GPU prep, Kubernetes | `ansible/`, `kubeadm/`, `.44` | `.44` remains canonical for live state. |
| Live Glasslab v2 manifests | `kubeadm/glasslab-v2/` | Apply from `.44`, not from the laptop. |
| Investigation and run control plane | `services/workflow-api/` | Owns investigation records, approval snapshots, evidence links, and bounded runs. |
| Workload catalog | `services/workflow-registry/` | Workloads should be registered here before execution. |
| Learning-task submission | `scripts/submit-learning-task.sh` | Thin wrapper around `POST /experiments/runs`. |
| Local model operator surface | `scripts/glasslab-opencode.sh` | Talks to exo's OpenAI-compatible endpoint. |
| Current CI policy | `docs/glasslab-v2/ci-policy-2026-07.md` | Push CI validates the current run-fabric path. |
| Metric-learning science code | `glasslab-metric-search` repo | Owns model, loss, miner, trainer, metrics, and workload image. |

## Component Status

### Current

These should remain in the default docs, default CI, and normal operator path.

| Component | Path | Role |
| --- | --- | --- |
| workflow-api | `services/workflow-api/` | Investigations, plan approvals, run records, validation, submission, evidence links, and comparison endpoints. |
| workflow-registry | `services/workflow-registry/` | Approved workload definitions. |
| research workspace runner | `services/research-workspace-runner/` | Digest verification and bounded execution for agent-generated CPU research code. |
| Postgres | `kubeadm/glasslab-v2/postgres/` | Durable records. |
| MinIO | `kubeadm/glasslab-v2/minio/` | Object-style infrastructure; useful, but not the whole state model. |
| NATS | `kubeadm/glasslab-v2/nats/` | Event/service primitive for bounded agents. |
| submit-learning-task | `scripts/submit-learning-task.sh` | Primary learning-task helper. |
| glasslab-opencode | `scripts/glasslab-opencode.sh` | Primary model-backed operator helper. |

### Secondary

These may remain live, but they should not define the product.

| Component | Path | Decision |
| --- | --- | --- |
| research-command-router | `services/research-command-router/` | Compatibility router; do not add new workflow logic here. Held pending confirmation on the #173 workflow-api auth work. |
| intake / interpretation / design / assessment agents | `services/*-agent/` | Bounded agents, not the core execution engine. |
| schedule-worker | `services/schedule-worker/` | Useful for scheduled execution; keep behind workflow-api records. |

### Compatibility-Only

These should stay callable for now, but new work should not build on them.

| Surface | Replacement |
| --- | --- |
| literature-first session startup routes | `POST /experiments/runs` or the explicit session/plan/run loop |
| paper intake queues | source intake plus bounded run specs |
| OpenClaw-era tool callers | `scripts/glasslab-opencode.sh` and repo-owned run scripts |
| `latest` as the main operator story | explicit session/run IDs |
| v1 Titanic agent stack | generic experiment workflow records |

### Historical

These are useful for context but should not be treated as current defaults.

| Area | Where to look |
| --- | --- |
| whatsapp-gateway, whatsapp-web-bridge, research-ingress (retired, issue #159) | `docs/glasslab-v2/historical/README.md` |
| March live-state snapshots | `docs/glasslab-v2/live-state-*.md` |
| old OpenClaw/Ollama/vLLM direction | `docs/glasslab-v2/historical/README.md` |
| early research-assistant framing | `docs/glasslab-v2/historical/README.md` |
| old one-off validation notes | keep only when linked by a current runbook |

## Current Learning-Task Flow

Use this path for new work:

```text
operator asks local model
  -> local model inspects repo/docs
  -> local model calls repo script
  -> script submits workflow-api run
  -> workflow-api validates registry entry
  -> workflow-api creates one bounded Kubernetes Job
  -> workload writes metrics/artifacts
  -> workflow-api records result
  -> evaluator/autoresearch compares under explicit contract
```

The detailed execution map is:

```text
docs/glasslab-v2/learning-task-flow.md
```

## CI/CD Shape

Default push CI should stay narrow:

- syntax check for `services/**/*.py`
- workflow-api core tests
- current YAML/JSON config parsing
- CodeQL

Manual CI is allowed to cover:

- WhatsApp and router adapters
- evaluator/reporter services
- heavyweight runner tests
- older compatibility surfaces

The policy is:

```text
docs/glasslab-v2/ci-policy-2026-07.md
```

## Simplification Backlog

### Phase 1: Stop the confusion

- Keep `README.md`, `docs/glasslab-v2/README.md`, and
  `docs/glasslab-v2/current/README.md` as the first three navigation points.
- Add a status header to stale docs when they are not current.
- Stop adding new docs that describe alternate command surfaces unless they are
  explicitly marked secondary or compatibility-only.
- Keep default CI aligned with the current run-fabric path.

### Phase 2: Prune API surface

- Remove test dependence on literature-first routes.
- Replace useful source intake behavior with smaller session/source endpoints.
- Delete or hide paper queue endpoints once no current script uses them.
- Stop teaching `latest` endpoints as the operator path.

### Phase 3: Prune services and manifests

- Done: WhatsApp (gateway, web bridge) and research-ingress are retired
  (issue #159); source, manifests, and CI entries removed.
  research-command-router is held pending confirmation on the #173
  workflow-api auth work.
- Keep bounded agents only if they consume/produce workflow-api records.
- Archive v1 Titanic manifests after the generic experiment path covers the same
  demonstration value.

### Phase 4: Simplify deployment

- Keep `.44` as the live deployment source until the registry/image path is
  fully reliable.
- Prefer SHA-tagged GHCR images over node-local imports.
- Keep one rollout helper per live service, with smoke validation built in.
- Avoid adding new ad hoc Kubernetes Jobs unless they are registered workloads
  or clearly named operational maintenance jobs.

## Decision Rules

When adding or changing something, ask:

1. Does this serve the primary learning-task path?
2. Is this generic cluster infrastructure, or workload-specific science code?
3. Does this write durable state through workflow-api/Postgres?
4. Is there already a service/script doing the same job?
5. Should this be current, secondary, compatibility-only, or historical?

If the answer is unclear, do not add another path. Extend the current path or
document the reason for a deliberate exception.
