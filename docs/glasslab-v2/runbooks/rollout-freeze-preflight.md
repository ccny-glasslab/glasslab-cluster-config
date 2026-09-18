# Rollout-Freeze Preflight

Rolling out the research orchestrator replaces its pod. A replacement during an
active run kills the agent turn that is in flight (exit 143) and can park the
run in `PAUSED`. Run this preflight before any rollout, image update, or
restart.

## The Invariant

Never deploy, roll out, or restart the research orchestrator while any run is
non-terminal.

A run is non-terminal in every state except `COMPLETE`, `FAILED`, `CANCELLED`,
and `TIMED_OUT`. That includes `PAUSED`.

The same freeze applies to `workflow-api`: a run mid-job-submission depends on
it. In practice the orchestrator is always in scope, because `scripts/rollout-research-services.sh
--service all` and `--service workflow-api` both roll the orchestrator first
(the authenticated bundle rolls the caller before the server). Only `--service
rabbitmq` leaves the orchestrator alone, and it still restarts the broker, so
do not use it as a loophole during a run.

## Why A Restart Is Destructive Mid-Run

- A rollout replaces the pod and the old container receives `SIGTERM`. An
  in-flight agent turn exits non-zero, observed as exit 143.
- On startup, `engine.recover()` marks the interrupted turn, rotates that
  agent's session, and resubmits the bounded turn. If recovery fails, the run
  is parked in `PAUSED` and needs a manual resume.
- `one_active_run=True` means a parked run holds the single active-run slot. No
  new run can start until the parked run is resumed or auto-cancelled.
- Kubernetes Jobs are durable. `JOB_QUEUED` and `JOB_RUNNING` are reconciled
  from the cluster on startup, so a restart does not lose submitted jobs. Agent
  turns are the part that is lost.

## Preflight (Read-Only)

Both steps are reads. Run them on the provisioner before you roll out.

### 1. Confirm the namespace is healthy

```bash
ssh glasslab-provisioner
sudo -n env KUBECONFIG=/home/glasslab/.kube/config \
  kubectl -n glasslab-v2 get pods -o wide
```

### 2. List non-terminal runs

Run this from the same provisioner session. It executes a short read-only
Python program inside the orchestrator pod and calls the local operator API at
`http://127.0.0.1:8080/runs`. The token is read from the pod's own environment
and is never printed.

```bash
sudo -n env KUBECONFIG=/home/glasslab/.kube/config \
  kubectl -n glasslab-v2 exec -i deploy/glasslab-research-orchestrator \
  -c orchestrator -- python3 - <<'PY'
import json, os, urllib.request
from datetime import datetime, timezone

TERMINAL = {"COMPLETE", "FAILED", "CANCELLED", "TIMED_OUT"}
token = os.environ.get("GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN")
headers = {"X-Glasslab-Operator-Token": token} if token else {}
request = urllib.request.Request("http://127.0.0.1:8080/runs", headers=headers)
runs = json.load(urllib.request.urlopen(request))["runs"]

now = datetime.now(timezone.utc)
active = [run for run in runs if run["state"] not in TERMINAL]
if not active:
    print("PASS: no non-terminal runs")
    raise SystemExit(0)

print("STOP: non-terminal runs present, defer the rollout")
for run in active:
    updated = run["updated_at"].replace("Z", "+00:00")
    age = now - datetime.fromisoformat(updated)
    stale = run["state"] == "PAUSED" and age.days >= 3
    print(
        f"  {run['run_id']}  {run['state']}  "
        f"updated {age.days}d ago"
        + ("  (stale PAUSED)" if stale else "")
    )
raise SystemExit(1)
PY
```

The operator token is required: every endpoint except `/health` and `/ready` is
token gated, including `/runs`. The token is read from the pod's own
environment and is never printed or passed on the command line.

### Pass condition

`PASS: no non-terminal runs`. Anything else is a stop. Do not roll out until
the check passes.

## If A Run Is Non-Terminal

Defer the rollout. Do not restart the orchestrator.

Distinguish a live run from a stale `PAUSED` record:

- **Live.** Any non-terminal, non-`PAUSED` state. An agent turn or job watch is
  in progress, or the run is waiting on a human approval. Wait for a terminal
  state, or pause it deliberately with `/research-pause` before you start.
- **Live `PAUSED`.** `updated_at` is inside the staleness window. It is not
  running a turn, but it still holds the active-run slot and represents pending
  work. Treat it as live.
- **Stale `PAUSED`.** `updated_at` is older than the staleness window. It is
  abandoned. It is auto-cancelled only lazily, the next time a new run is
  created, so it can still appear in `/runs`. Resolve it before deploying, with
  `POST /runs/<run_id>/cancel`, or let the next `/task-start` reclaim the slot.

The staleness window is `paused_run_staleness_days`, default 3. It is not
overridden in the tracked ConfigMap, so the default applies unless the live
deployment overrides it. Confirm before you rely on it:

```bash
sudo -n env KUBECONFIG=/home/glasslab/.kube/config \
  kubectl -n glasslab-v2 get configmap glasslab-research-orchestrator-config \
  -o jsonpath='{.data.GLASSLAB_ORCHESTRATOR_PAUSED_RUN_STALENESS_DAYS}{"\n"}'
```

An empty result means the default of 3 days is in effect.

## If You Deploy Anyway

- The active agent turn is killed (exit 143) and its output is lost.
- Recovery rotates the agent session. The run either continues its bounded turn
  automatically or is parked in `PAUSED`.
- A parked run needs a manual resume, through `/research-resume` in Discord or
  `POST /runs/<run_id>/resume` with the operator token.
- Until it is resumed or auto-cancelled, `one_active_run` blocks every new run.

## See Also

- `deploy-v2.md`
- `rollback-v2.md`
- `docs/research-orchestrator.md` (recovery and state machine)
