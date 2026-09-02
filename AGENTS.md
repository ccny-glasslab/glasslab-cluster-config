# Glasslab Agent Handoff

This file is the first stop for coding agents working with Glasslab. Read it
before inferring architecture from older documents or changing live systems.
It contains stable operating rules. Read `HANDOFF.md` for summarized current
state and `TODO.md` for links into the authoritative GitHub Issues work queue.

## Authority And Vocabulary

There are three different kinds of truth:

1. **Committed state** is the GitHub repository.
2. **Documented live state** is the last checked snapshot in the docs.
3. **Actual live state** must be queried from the provisioner and cluster.

Do not describe documented state as current without checking it live.

Use these names consistently:

- **Glasslab**: the overall lab and project, not a host.
- **gateway**: public SSH entry host `glasslab.org`, hostname `glasslab`.
- **provisioner**: PXE, Ansible, canonical repo, and `kubectl` host at
  `192.168.1.44`, hostname `glasslab-PXE-01`.
- **control plane**: Kubernetes host `cp01` at `192.168.1.49`.
- **workers**: Kubernetes `node01` through `node05`.
- **shared `glasslab` account**: a legacy Unix administrator identity, not a
  machine name. Prefer personal accounts.

Normal access is:

```text
contributor workstation
  -> gateway
  -> provisioner
  -> Kubernetes API or Ansible-managed nodes
```

Use `ssh glasslab-gateway` and `ssh glasslab-provisioner` when those aliases
are installed. The canonical live checkout is:

```text
/home/glasslab/cluster-config
```

A laptop checkout is a client copy. Never use it alone to make a claim about
live pods, secrets, ignored files, imported images, or runtime data.

## Current Product

The current human-facing research path is:

```text
Discord
  -> research-orchestrator
  -> isolated OpenCode-backed Honeydew and Beaker runtimes
  -> structured, policy-checked action requests
  -> workflow-api
  -> bounded Kubernetes Jobs
  -> immutable evaluator and artifact records
  -> Discord status, approvals, and report delivery
```

The responsibilities are deliberately separate:

- **Honeydew** drafts `program.md`, reviews methodology and implementation,
  verifies evidence, and writes `report.md`.
- **Beaker** writes the implementation and candidate configuration, runs
  bounded local checks, proposes experiment matrices, and analyzes job results.
- **research-orchestrator** owns state transitions, approvals, agent sessions,
  policy, recovery, job watching, and the append-only event log.
- **workflow-api** is the bounded cluster execution control plane.
- **evaluation contracts** are immutable to both agents and decide whether job
  evidence satisfies the approved contract.
- **Discord** is an interface and transcript projection, not authoritative
  memory or state.

Both agents run through the OpenCode agent runtime against the exo
OpenAI-compatible endpoint configured for the shared Qwen model. Their
workspaces and runtime data are isolated per run and agent. Hermes remains
available only as an explicit opt-in rollback backend, selected by setting
`GLASSLAB_ORCHESTRATOR_AGENT_RUNTIME_BACKEND` to `hermes`; the live
orchestrator configmap selects `opencode` (verified 2026-09-02).

Run state is stored in PostgreSQL in production, selected by
`GLASSLAB_ORCHESTRATOR_STORE_BACKEND=postgres`. SQLite remains the local, test,
and import-migration backend (`sqlite`).

Primary code and deployment areas:

- `services/research-orchestrator/`
- `services/research-workspace-runner/`
- `services/workflow-api/`
- `kubeadm/glasslab-v2/research-orchestrator/`
- `docs/research-orchestrator.md`
- `docs/research-orchestrator-command-surface.md`

The following are not the current research front door:

- `services/agent-api`, `services/runner`, and `kubeadm/agent-stack` are the
  legacy Titanic v1 reference implementation.
- OpenClaw and WhatsApp material is compatibility or historical context unless
  a current task explicitly targets those adapters.
- The older `!new`, `!plan`, and related command vocabulary does not describe
  the active Honeydew/Beaker Discord slash-command surface.

## Discord Workflow

Start an attached research task in the configured Glasslab channel:

```text
/task-start archive:<zip> objective:<optional narrower objective>
```

Start a question-driven run without a task archive:

```text
/research-start objective:<research objective>
```

Upload local data before starting a task:

```text
/dataset-upload dataset:<file> name:<stable-name> role:<role> contains_labels:<bool>
```

Put the returned `glasslab-dataset://<sha256>` reference in `problem.md` or the
research objective. Do not embed large datasets in the task ZIP.

The expected lifecycle is:

1. Honeydew drafts the protocol and evaluation-contract proposal.
2. A human approves or rejects the protocol in Discord.
3. Beaker plans and implements the bounded workload.
4. Deterministic preflight and Honeydew review the implementation and matrix.
5. A human approves or rejects cluster execution.
6. The orchestrator submits bounded jobs and records authoritative status.
7. Beaker analyzes results; Honeydew independently verifies them.
8. Honeydew writes the report; a human accepts or rejects it.

Use these controls inside a run thread:

```text
/research-pause
/research-resume
/research-cancel
/research-artifacts
```

Approve and Reject are Discord buttons, not slash commands. A failed job is
normally evidence for Beaker to analyze and revise; it is not automatically a
failed research run. `FAILED`, `CANCELLED`, and `TIMED_OUT` are terminal states
and cannot currently be resumed through `/research-resume`.

Read `docs/research-orchestrator-command-surface.md` before operating a run.

## Scientific And Execution Boundaries

- Agent prose is not evidence that an action occurred.
- Only durable job, event, artifact, and evaluator records are authoritative.
- Neither agent receives raw `kubectl`, SSH, cluster credentials, secrets,
  image publication, Git push, or unrestricted job submission.
- A model proposes normalized actions. Deterministic code renders and submits
  the final job.
- The workload emits metrics and evidence. It must not create or score
  `evaluation.json`, `integrity_pass`, or `rubric_score`.
- Every contract-required workload artifact must be produced. Preflight checks
  source references before cluster submission; the evaluator verifies actual
  files afterward.
- Matrix seeds create separate cluster jobs. If one job already performs an
  internal multi-seed stability analysis, use one outer matrix seed instead of
  duplicating the internal seed list.
- Do not claim scientific confirmation from one exploratory run. Promote
  promising results into a frozen, multi-seed confirmatory campaign.

## Inspecting A Live Run

First check the service and cluster from the provisioner:

```bash
ssh glasslab-provisioner
sudo -n env KUBECONFIG=/home/glasslab/.kube/config \
  kubectl -n glasslab-v2 get pod,job -o wide
```

The orchestrator is internal-only. Port-forward it from a contributor
workstation:

```bash
ssh -L 18080:127.0.0.1:18080 glasslab-provisioner \
  'sudo -n env KUBECONFIG=/home/glasslab/.kube/config \
   kubectl -n glasslab-v2 port-forward \
   svc/glasslab-research-orchestrator 18080:8080'
```

Then inspect authoritative read APIs:

```bash
curl -fsS http://127.0.0.1:18080/runs | jq
curl -fsS http://127.0.0.1:18080/runs/<run-id>/events | jq
curl -fsS http://127.0.0.1:18080/runs/<run-id>/artifacts | jq
curl -fsS http://127.0.0.1:18080/runs/<run-id>/turns | jq
```

Per-run durable files are mounted in the orchestrator pod at:

```text
/mnt/artifacts/research-orchestrator/runs/<run-id>/
  protocol/
  beaker-worktree/
  honeydew-worktree/
  shared-artifacts/
  reports/
  events/
  runtime/beaker/
  runtime/honeydew/
```

The shared PVC is backed by NFS. Inspect workspaces through the pod rather than
assuming that path exists on the provisioner's local filesystem.

`protocol/`, `reports/`, `shared-artifacts/`, and `events/` hold durable,
artifact- and report-referenced material and are never touched by cleanup.
`beaker-worktree/`, `honeydew-worktree/`, and `runtime/<agent>/` are agent
process scratch space (git worktrees, OpenCode/Hermes session state and
logs); nothing in the run/artifact database ever references a path inside
them. OpenCode's own package/model download cache lives outside the per-run
tree entirely, at the shared `opencode_shared_cache_root` (one copy for every
run and both agents, since it is the same OpenCode version and plugin set
each time) rather than being copied per run.

Once a run reaches a terminal state (`COMPLETE`, `FAILED`, `CANCELLED`,
`TIMED_OUT`) its scratch space is eligible for cleanup after
`terminal_run_retention_days` (default 14). Run it manually from the
orchestrator pod or a workstation with the same `GLASSLAB_ORCHESTRATOR_*`
settings:

```bash
python services/research-orchestrator/scripts/cleanup-run-storage.py
python services/research-orchestrator/scripts/cleanup-run-storage.py --apply
```

The default (and `--dry-run`) only reports what would be freed; `--apply` is
required to actually delete anything. See
`services/research-orchestrator/app/storage_retention.py` for the full
safety design, including the per-subdirectory check against live artifact
records applied immediately before every deletion.

## Development And Delivery

Work on one branch per coherent change and open a feature pull request into
`testing`. Promote `testing` to protected `main` only when the integration
state is ready for production. Direct administrator pushes are for incident
recovery only.

Before pushing:

```bash
./scripts/check-before-push.sh
```

Useful narrower checks:

```bash
./scripts/check-before-push.sh --docs
./scripts/check-before-push.sh --configs
./scripts/check-before-push.sh --python-core
```

GitHub Actions publishes service images under the full commit SHA. After a
merged service change, deploy the exact release from the canonical provisioner
checkout:

```bash
cd /home/glasslab/cluster-config
./scripts/rollout-research-services.sh --sync
```

Do not build a second deployment path or manually mutate tracked manifests to
work around this release flow.

## Security And Collaboration Rules

- Never commit passwords, private keys, API tokens, Discord credentials,
  webhook URLs, kubeconfigs, or live secret manifests.
- Do not copy secrets into prompts, issues, pull requests, screenshots, or
  shell history.
- Use personal accounts and the provisioner-managed access policy.
- Contributors submit research through the orchestrator; they do not normally
  SSH to workers or receive unrestricted cluster-admin credentials.
- Preserve unrelated local changes on the provisioner. Inspect `git status`
  before syncing or deploying.
- Prefer current docs and code over historical snapshots. When documents
  conflict, identify the conflict instead of silently choosing the convenient
  version.

## Reading Order

Two coding agents edit this repository concurrently. Before touching a shared
file (see `docs/agent-coordination.md` — the lane map and keep-both conflict
rule), read that document.

1. `HANDOFF.md`
2. `TODO.md`
3. `README.md`
4. `docs/research-orchestrator-command-surface.md`
5. `docs/research-orchestrator.md`
6. `docs/access-topology.md`
7. `docs/contributor-access.md`
8. `CONTRIBUTING.md`
9. `docs/glasslab-v2/current/README.md`

For infrastructure work, additionally read the relevant Ansible playbooks and
`docs/glasslab-v2/runbooks/`. For legacy behavior, read historical documents
only after understanding the current path above.
