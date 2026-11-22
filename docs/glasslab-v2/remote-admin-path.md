# Remote Admin Path

This note clarifies an important operational distinction for issue `#21`.

Reducing `.44` dependence does **not** have to mean removing `.44` as the canonical admin host.

It can also mean making `.44` reachable and usable from outside the lab without changing the cluster control model.

## Current Recommended Control Model

Keep:

- `.44` as the canonical apply and validation host
- `glasslab.org` as the public SSH gateway
- live cluster claims grounded in checks from `.44`

But also support:

- remote operator access to `.44` through an approved hop path

That preserves the current trust model while reducing the practical friction of needing to be physically on the lab network.

## Current Working Hop

The currently working remote path is:

- home client
- `gr66ss-glasslab@ssh.glasslab.org` (`glasslab-gateway`)
- `gr66ss-glasslab@192.168.1.44` (`glasslab-provisioner`)

From `.44`, the operator can then reach:

- cluster admin context via `kubectl`
- internal worker nodes
- Mac service hosts such as `.23` and `.12`

Thin helper wrappers now exist in the repo so this hop does not need to be rebuilt by hand every time:

```bash
./scripts/run-on-provisioner.sh hostname
./scripts/kube-on-provisioner.sh get pods -n glasslab-v2
./scripts/check-openclaw-turn.sh
```

These helpers use the configured key-based `glasslab-provisioner` SSH alias,
whose `ProxyJump` traverses `glasslab-gateway`. They standardize the remote
path for repeatable rollouts and log inspection.

## Why This Matters

This changes the operational meaning of issue `#21`.

The question is no longer only:

- "how do we make OpenClaw deployment stop depending on `.44`?"

It is also:

- "which parts of `.44` dependence are actually harmful once remote access to `.44` exists?"

For now, the answers are:

- runtime export friction: worth reducing
- secret-local material: intentionally still local
- `.44` as canonical admin context: still acceptable
- inability to operate from off-site: now materially improved

## What This Does Not Solve

The remote hop does not by itself solve:

- secret backup
- secret rotation
- contributor identity reconciliation across managed hosts
- replacing the gateway or provisioner with a different admin model

Those remain separate infrastructure questions.

## Recommendation

Treat the remote-admin path as a reduction in **operational friction**, not a reason to eliminate `.44` centrality prematurely.

The near-term goal should be:

1. keep `.44` canonical
2. keep runtime export/apply boring and reviewable
3. improve remote access to `.44`
4. only later decide whether `.44` should stop being the canonical admin host

## References

- `openclaw-runtime-portability.md`
- `provisioner-dependence-inventory.md`
- `operator-access-recommendation.md`
