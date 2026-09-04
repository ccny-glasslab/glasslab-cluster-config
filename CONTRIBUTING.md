# Contributing To Glasslab

Glasslab is being simplified around one primary learning-task path:

```text
OpenCode -> exo -> repo-owned scripts -> workflow-api -> Kubernetes Job
```

Contributions should either strengthen that path or clearly label themselves as
secondary, compatibility-only, or historical.

## Start Here

Read these first:

- `AGENTS.md`
- `HANDOFF.md`
- `TODO.md`
- `README.md`
- `docs/access-topology.md`
- `docs/contributor-access.md`
- `docs/glasslab-v2/current/README.md`
- `docs/glasslab-v2/system-map-2026-07.md`
- `docs/glasslab-v2/ci-policy-2026-07.md`
- `docs/glasslab-v2/learning-task-flow.md`

## Before Pushing

Run the default local check:

```bash
./scripts/check-before-push.sh
```

Narrower modes are available when you are making a small scoped change:

```bash
./scripts/check-before-push.sh --docs
./scripts/check-before-push.sh --configs
./scripts/check-before-push.sh --python-core
```

The default check mirrors the default CI signal:

- current YAML/JSON parsing
- Markdown link resolution
- shell syntax for primary operator scripts
- Python syntax for services
- workflow-api core tests

## Pull Request Flow

Create one branch for one coherent change and open a pull request into
`testing`. Continue pushing revisions to the same branch; GitHub updates the
pull request automatically. Promote `testing` to `main` only when the
integration state is ready for production.

Start substantive work from a GitHub issue. The issue is the authoritative
record for scope, status, acceptance criteria, dependencies, and discussion;
`TODO.md` is only a short priority index. Before coding:

1. Check for an existing issue.
2. Create one with the work-item template if none exists.
3. Apply one `priority:*`, one `state:*`, and the relevant `area:*` labels.
4. Comment when taking ownership and link the working branch or pull request.

Use `Closes #<issue>` in the pull request when the merge fully resolves the
work. If it only contributes to a larger issue, use `Refs #<issue>` and leave
the remaining acceptance criteria visible on the issue.

`testing` and `main` are protected. A merge requires:

- the always-running `Glasslab PR Gate` check
- one approval from someone other than the author
- all review conversations resolved
- a current approval after material revisions

`main` is the production branch and receives changes only by promoting
reviewed `testing` state.

Use squash merge, then delete the feature branch. Direct administrator pushes
are an incident-recovery bypass, not a normal development path.

## CI Lanes

Every pull request runs the four reusable CI lanes below. Their results are
combined into the required `Glasslab PR Gate` check. Path-aware copies still
run after relevant changes land on `testing` or `main`, and compatibility
tests remain manual.

| Lane | Purpose |
| --- | --- |
| `CI Python` | service Python syntax and workflow-api core tests |
| `CI Configs` | YAML/JSON syntax for current configs |
| `CI Docs` | local Markdown link checks |
| `CI Scripts` | shell syntax for repo scripts |
| `CodeQL` | GitHub code scanning |

Manual compatibility tests remain available through `workflow_dispatch` on
`CI Python`. Use them when touching adapters, reporter/evaluator, or heavyweight
runner code.

## Ownership Boundaries

Use `cluster-config` for:

- physical lab and Kubernetes infrastructure
- workflow-api and workflow-registry
- generic run submission, records, comparisons, and deployment scripts
- current system docs and runbooks

Use workload repos such as `glasslab-metric-search` for:

- dataset protocols
- model/loss/miner/trainer code
- emitted metrics schema
- workload image build context

Do not add Kubernetes topology, WhatsApp routing, or global run records to a
scientific workload repo.

## Live State

The laptop checkout is not authoritative for live state.

- `.44` is the canonical provisioner and live operations checkout.
- GitHub is committed repo state.
- Docs are documented state.
- Only `.44` can confirm actual live cluster state.

Relevant merges to `main` publish a matched pair of service images to GHCR
under the full commit SHA. If a change affects a live service, wait for the `Publish Service Images`
workflow, then roll that exact commit from `.44`:

```bash
ssh glasslab-provisioner
cd /home/glasslab/cluster-config
./scripts/rollout-research-services.sh --sync
```

Use `--service workflow-api` or `--service research-orchestrator` for a
single-service rollout. The old workflow-api helper is a compatibility wrapper;
it no longer builds images locally.

## Documentation Freshness (TTL)

Every document that describes current behavior carries a `Last verified:
<date>` marker near the top. The marker is the date the document's claims were
last checked against the live system or committed state.

- A document is stale 30 days after its `Last verified` date unless someone
  refreshes the marker.
- When a document's claims are falsified by the live system or committed
  state, add a `STALE` banner at the top explaining what changed and when.
- A falsified document is moved to `docs/glasslab-v2/historical/` so readers
  find it intentionally instead of treating it as current.
- Refresh the marker when you re-verify the document still describes current
  behavior, or update the document and re-verify.

## Pull Request Expectations

A useful PR should say:

- what area it changes
- whether it touches the primary path or a secondary/compatibility path
- what local checks were run
- whether live rollout is required
- what docs changed if behavior changed

When in doubt, prefer a small PR that improves the current path over a broad PR
that adds another competing path.
