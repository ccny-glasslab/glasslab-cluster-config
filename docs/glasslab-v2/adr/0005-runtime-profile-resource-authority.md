# ADR 0005: Runtime Profile Resource Authority

Status: accepted

Issue: [#499](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/499)

Sources:

- [Resource authority: profiles, precedence, and the recommended rule](../../research-orchestrator-spec-fields.md#5-resource-authority-profiles-precedence-and-the-recommended-rule)
  - the #492 audit analysis (finding F9) that this record turns into a decision;
- profile-introducing commits `4aaeca5` ("Add bounded ML benchmark
  orchestration"), `54a5f58` ("Generalize research task orchestration"), and
  `8b2197f` ("Rebind task runtimes and dataset names") - all three have empty
  commit bodies, which is why this record had to be written after the fact;
- [Compiled Research Tasks](../../research-orchestrator.md) and the
  [task-bundle guide](../../research-orchestrator-task-bundle-guide.md).

## Context

### Why runtime profiles exist

Every imported task bundle compiles to exactly one platform-selected runtime
profile (`RuntimeProfile`, `services/research-orchestrator/app/task_bundles.py:89-125`):

| Profile | `workload_id` | cpu | memory_gib | gpus | wallclock_minutes |
| --- | --- | --- | --- | --- | --- |
| `cpu-ml-standard-v1` | `workspace-cpu-ml-v1` | 4 | 8 | 0 | 60 |
| `gpu-ml-standard-v1` | `workspace-gpu-ml-v1` | 8 | 32 | 1 | 240 |

`runtime_profile` is a closed `Literal` in the compiled spec
(`services/research-orchestrator/app/schemas.py:286`), and the compiler resolves
it to the profile's `workload_id`, digest-pinned runner image, and resource
tuple (`services/research-orchestrator/app/task_bundles.py:481`, `:564-572`).

Profiles exist for three reasons:

1. **Bounded execution and a security boundary.** The compiler model "does not
   select a container image, command, workload ID, resource ceiling, evaluator
   entry point, or Kubernetes fields" (`docs/research-orchestrator.md:454-456`).
   The profile is deterministic code's answer to all of them. The workload it
   selects runs with `network_policy: none`
   (`services/research-orchestrator/app/cluster.py:260`) and an unprivileged
   container - no privilege escalation, all capabilities dropped
   (`services/workflow-api/app/job_submission.py:713-717`) - and the runner
   image must be digest-allowlisted.
2. **Predictable scheduling and quota safety.** The workflow registry restates
   each profile as Kubernetes requests/limits and a `max_wallclock_minutes`
   (`services/workflow-registry/definitions/workspace-cpu-ml-v1.json`,
   `workspace-gpu-ml-v1.json`), and workflow-api renders exactly those into the
   Job (`services/workflow-api/app/job_submission.py:602-608`). Enumerable
   profiles keep jobs schedulable on a fixed cluster and keep concurrent runs
   from starving each other.
3. **Benchmark comparability.** Imported benchmarks rerun at identical
   resources, so results stay comparable across runs and seeds.

### Threat model

The agent side is not trusted with cluster-shaping decisions. Task material,
objectives, and agent output can carry prompt injection or plain model error; a
compromised or confused compiler turn must not be able to escalate
CPU/GPU/memory, extend wall-clock, choose an arbitrary image or command, or set
Kubernetes fields. The profiles, the digest allowlist, the policy clamp,
`network_policy: none`, and the unprivileged container together bound the blast
radius of whatever runs. **The security boundary is the reason profiles exist;
resource accuracy is not.**

### What was accidentally decided instead

The profile was implemented as an exact-match authority: for imported tasks,
`matrix.resources` must equal the compiled profile dict
(`services/research-orchestrator/app/engine.py:5256-5266`), while the contract's
`resource_constraints` independently required the matrix to fit inside it
(`services/research-orchestrator/app/matrix.py:43-55`). Two independent ceilings
made a contract stricter than the platform profile unsatisfiable - the Titanic
contract declared 30 wall-clock minutes against `cpu-ml-standard-v1`'s 60
([#483](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/483)).
Because no record stated which authority wins, the contradiction was invisible
until a live run hit it.

## Decision

Runtime profiles are **platform-selected envelopes and defaults, not fixed
contracts**. A task may not invent resources, but it may request less than what
the platform envelope allows.

Resolution precedence, evaluated per dimension - the single rule:

```text
effective = min(request, profile.envelope, contract.constraints, policy)
```

- `profile.envelope` is the platform default and the maximum for an imported
  benchmark (`RUNTIME_PROFILES`,
  `services/research-orchestrator/app/task_bundles.py:96-125`).
- `contract.constraints` is a compatibility envelope, not an independent
  ceiling. `contract.constraints >= profile.envelope` is validated **once at
  promotion** (and re-checked at binding), so a promoted contract can never be
  the stricter of two ceilings again.
- `policy` is the outermost hard clamp, applied when actions are classified
  (`services/research-orchestrator/app/config.py:265-268`;
  `services/research-orchestrator/app/policy.py:146-156`: `cpu<=8`,
  `memory_gib<=32`, `gpus<=1`, `parallel<=4`).
- A request **above** the profile envelope requires an explicit, recorded human
  approval naming the dimension and both values, and the contract must already
  allow it. Nothing silently upsizes.
- The matrix either copies the profile or requests less; it may never exceed
  the resolved envelope.

## Current implementation state

The decision above is the policy. The code enforces the fail-closed half of it;
the clamp half is not implemented yet:

- Compile resolves and records the profile
  (`services/research-orchestrator/app/task_bundles.py:481`, `:564-572`).
- For imported tasks, matrix resources must currently **equal** the profile
  exactly (`services/research-orchestrator/app/engine.py:5262-5266`) rather
  than being allowed to request less. This is the remaining gap between the
  decision and the code.
- A profile that does not fit the contract is a configuration contradiction,
  detected by `profile_contract_resource_conflicts`
  (`services/research-orchestrator/app/preflight.py:409-437`) and routed to
  fail-closed handling at all entry points: contract candidate seal
  (`services/research-orchestrator/app/engine.py:3703-3730`), installed-contract
  binding and candidate promotion
  (`services/research-orchestrator/app/engine.py:4063-4104`,
  `_fail_closed_on_contract_profile_conflict`; the pre-#490 resume loop on the
  binding path was fixed in `243e2bb`), and matrix preflight as a non-retryable
  error (`services/research-orchestrator/app/engine.py:5222-5240`).
- Every action is clamped by global policy before approval
  (`services/research-orchestrator/app/policy.py:146-156`).

The exact-match requirement is an implementation detail of the current
enforcement layer, not a rival decision. A future change that allows smaller
requests must keep the same single authority - the profile envelope - and must
not reintroduce an independent contract ceiling. This record exists so that such
a change cannot silently redefine the policy, which is the failure mode #483
exploited.

## Consequences

- A task bundle can never choose its own image, command, workload ID, or
  Kubernetes fields; the platform profile remains the only source for all of
  them.
- Contracts are reviewed once against the envelope rather than at every matrix
  revision, removing the 60-vs-30 class of dead ends.
- Comparability is preserved: a benchmark that does not request less still runs
  at the profile tuple, and the job is rendered from the registry's profile
  resources (requests/limits) rather than from agent-authored values.
- Smaller requests are not yet accepted for imported tasks; until the clamp
  lands, the exact-match requirement stands and any profile/contract
  contradiction fails closed for human resolution.
