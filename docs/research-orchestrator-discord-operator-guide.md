# Research Orchestrator Discord Guide

Discord is the human control surface for a Glasslab research run. It projects
the durable PostgreSQL records and append-only event log; it is not agent
memory, job truth, or the source of authority. Honeydew and Beaker are Hermes
agents backed by the lab Qwen endpoint. Their raw model tokens and tool traces
are intentionally not posted to Discord.

Only the configured channel accepts commands. Approval controls require the
configured administrator role (`Mystic Arts Master`) or an explicitly allowed
user ID. The role ID, bot token, and operator token are local Kubernetes
secret data and must not be committed.

## Start A Run

Use the configured Glasslab channel.

```text
/research-start objective:<research question>
```

For an arbitrary packaged task, attach a ZIP containing `problem.md` and run:

```text
/task-start archive:<ZIP> objective:<optional narrower objective>
```

`/benchmark-start` is a compatibility alias for `/task-start`. New work
should use `/task-start`.

For local data, upload it before starting the task:

```text
/dataset-upload dataset:<file> name:<short-name> role:<train|test|reference>
```

The bot returns an immutable `glasslab-dataset://<sha256>` reference. Put that
exact reference in `problem.md`; the executor resolves and verifies it from
read-only shared storage. A task archive is an instruction bundle, not a way
to sneak unregistered data into a job.

## What Happens Next

1. The bot creates one thread and a live status message for the run.
2. Honeydew drafts `program.md`: question, hypothesis, controls, metrics,
   stopping conditions, evidence, and the proposed evaluation contract.
3. An administrator approves or rejects the protocol in that thread.
4. Beaker plans and implements the bounded workload, then proposes a normalized
   experiment matrix. It has no `kubectl`, SSH, secret, push, or registry
   access.
5. Honeydew reviews methodology and the immutable evaluation contract.
6. An administrator approves or rejects the execution request. The approval
   message states the variants, seeds, job count, resources, required
   artifacts, contract digest, and what improvement is being tested.
7. The orchestrator submits the approved matrix through `workflow-api`; the
   model runtime is idle while Kubernetes jobs run.
8. Beaker analyzes persisted job logs and artifacts. Honeydew independently
   verifies claims, writes `report.md`, and asks for final acceptance.

Pressing **Approve** authorizes exactly the stated action. It is not evidence
that a cluster job, evaluator, or report succeeded.

## Controls

Inside a run thread, commands infer the run ID. From the main channel, supply
`run_id` when the command offers it.

| Command | Effect |
| --- | --- |
| `/research-pause [run_id] [reason]` | Aborts an active Hermes turn, preserves workspaces and recovery state, and records the pause. |
| `/research-resume [run_id] [reason]` | Restarts workflow recovery from the recorded state. |
| `/research-cancel [run_id] [reason]` | Cancels the run, aborts active agent work, requests cancellation for active jobs, and records the actor and reason. A paused run may be cancelled. |
| `/research-artifacts [run_id] [include_source]` | Delivers a digest-verified archive of run artifacts and successful-job outputs. |

Reject buttons collect a reason. The reason becomes durable review feedback for
the appropriate agent; it must produce a follow-up state or failure message,
not leave the thread silent.

## Results And Failure

Use `/research-artifacts` from the run thread after jobs complete. The bundle
contains the protocol, report when available, evaluator output, metrics,
tables, manifests, logs, and generated analysis notebook when those records
exist. `artifact-manifest.json` records each delivered file's origin and
SHA-256 digest. The evaluator output, not the notebook or agent prose, is the
authoritative scientific evidence.

A failed Kubernetes job is normally an observation routed back to Beaker. A
bad preflight is rejected before submission. Terminal `FAILED`, `CANCELLED`,
and `TIMED_OUT` runs cannot yet be cloned or retried from Discord; start a new
bounded run after reviewing the evidence.

## Inspecting Without Discord

The service is internal-only. From a contributor workstation, tunnel through
the provisioner:

```bash
ssh -L 18080:127.0.0.1:18080 glasslab-provisioner \
  'sudo -n env KUBECONFIG=/home/glasslab/.kube/config kubectl -n glasslab-v2 \
   port-forward svc/glasslab-research-orchestrator 18080:8080'
```

Then inspect persisted state. Every read path except `/health` and `/ready`
requires the operator token:

```bash
TOKEN=<operator-token>
curl -fsS -H "X-Glasslab-Operator-Token: $TOKEN" \
  http://127.0.0.1:18080/runs | jq
curl -fsS -H "X-Glasslab-Operator-Token: $TOKEN" \
  http://127.0.0.1:18080/runs/<run-id>/events | jq
curl -fsS -H "X-Glasslab-Operator-Token: $TOKEN" \
  http://127.0.0.1:18080/runs/<run-id>/artifacts | jq
```

Do not place credentials in commands, shell history, screenshots, Discord, or
Git. For implementation boundaries and recovery details, see
[`research-orchestrator.md`](research-orchestrator.md) and the concise
[`research-orchestrator-command-surface.md`](research-orchestrator-command-surface.md).
