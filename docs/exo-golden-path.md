# exo Golden Path

Last updated: 2026-08-06

This is the current two-node exo, Thunderbolt RDMA, Qwen, and OpenCode path.
The older `.21` and `.19` topology is retired from this runbook.

## Topology

| Role | LAN | Thunderbolt | exo role |
| --- | --- | --- | --- |
| API/master | `192.168.1.17` | `192.168.0.2/30` | forced master and OpenAI-compatible API |
| Worker | `192.168.1.18` | `192.168.0.1/30` | worker bootstrapped directly to `.17` |

Both nodes use:

- exo tree: `/Users/glasslab/exo`
- namespace: `glasslab-rdma-prod`
- API port: `52415`
- libp2p port: `54216`
- RDMA interface: `rdma_en5`

The approved OpenCode model is
`mlx-community/Qwen3-Coder-Next-4bit`. Its placement must be two-node
`Pipeline + MlxJaccl`.

## Supervised Startup

System `LaunchDaemon` jobs own exo. Do not start another copy with `nohup`.

On `.17`:

- `com.glasslab.exo` supervises the exo master/API.
- `com.glasslab.exo-reconcile` waits for both RDMA-connected nodes and restores
  the approved Qwen placement when no active instance exists.

On `.18`:

- `com.glasslab.exo` supervises the worker.
- the worker obtains `.17`'s current peer ID from the master API and uses an
  explicit Thunderbolt bootstrap multiaddress.

`KeepAlive` restarts a process that exits. Both jobs run as the `glasslab`
account at boot and do not require an interactive login.

## Install Or Update

Copy `scripts/macos/` to each Mac and run:

```bash
sudo ./scripts/macos/install-exo-launchd.sh 18  # on .18 first
sudo ./scripts/macos/install-exo-launchd.sh 17  # on .17 second
```

Installing `.18` first lets the worker wait for the deterministic master while
`.17` is restarted.

## Validate

Check the daemons:

```bash
sudo launchctl print system/com.glasslab.exo
sudo launchctl print system/com.glasslab.exo-reconcile  # .17 only
```

Check the physical path on both Macs:

```bash
ifconfig en5
ibv_devices
```

Expected addresses:

```text
.17 en5: 192.168.0.2
.18 en5: 192.168.0.1
```

Check the authoritative state on `.17`:

```bash
curl -fsS http://127.0.0.1:52415/state | jq '{
  nodes: (.topology.nodes | length),
  connections: .topology.connections,
  instances: (.instances | keys)
}'
```

Run a real completion:

```bash
curl -fsS http://127.0.0.1:52415/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"mlx-community/Qwen3-Coder-Next-4bit","messages":[{"role":"user","content":"Reply exactly: OK"}],"max_tokens":8}'
```

## OpenCode

Use OpenCode from the contributor workstation. The repo launcher checks the
two-node RDMA topology and a real model completion before starting OpenCode.
When the workstation cannot reach the exo API directly, it opens a temporary
key-only SSH tunnel to `.17` and gives OpenCode that exact forwarded endpoint:

```bash
cd ~/cluster-config
./scripts/glasslab-opencode.sh
./scripts/glasslab-opencode.sh "Inspect the current issue list."
```

The launcher is self-contained. It does not depend on an untracked
`~/.local/bin/opencode-with-exo` helper or the user's global OpenCode provider
configuration. OpenCode is intentionally not installed on the provisioner:
untrusted agent work belongs in disposable workstation worktrees, away from
the canonical apply tree and cluster credentials.

The model reconciler may need a short interval after both Macs boot before the
instance is ready. Override `GLASSLAB_EXO_SSH_TARGET` only when the canonical
`glasslab-exo17` SSH alias is unavailable.

## Known Recurring Failure: RDMA Device Drift

Symptom: every chat completion returns `No instance found for
mlx-community/Qwen3-Coder-Next-4bit` and the reconcile log repeats
`waiting for the two-node rdma_en5 topology`, even though the Thunderbolt
link is up (pings fine, `/state` shows 2 nodes and 2 rdma links).

Root cause: macOS failed to register the Thunderbolt RDMA verbs device on a
Mac until reboot. Verify with:

```bash
ibv_devices        # expected: a rdma_en5 line on BOTH Macs
```

The failure is asymmetric and deterministic: the affected Mac lists
`rdma_en2/en3/en4` (empty ports) but no `rdma_en5` for the connected
Thunderbolt 4 port, so JACCL's queue-pair init fails (`errno 96`) and no
two-node placement can ever be created.

Repair (once): reboot the affected Mac. launchd auto-restores exo, and
`rdma_en5` re-registers (verified on `.17`, 2026-09-01).

Permanent behavior (deployed 2026-09-01):

- `com.glasslab.exo-reconcile` no longer spins in silence: when the pair is
  unavailable it logs a `REPAIR REQUIRED` line (with the `ibv_devices`
  diagnostic) and after `GLASSLAB_EXO_PAIR_GRACE_CHECKS` (default 3) checks
  it submits a **single-node** placement of the approved model, so the
  orchestrator always has its model even while the pair is degraded. The
  two-node path is re-tried every cycle and the fallback is replaced
  automatically when the pair returns.
- `com.glasslab.exo-rdma-guard` (both Macs, every 5 min) makes drift loud
  immediately: it checks `ibv_devices` for `rdma_en5` and appends a
  `REPAIR REQUIRED` line to
  `/Users/glasslab/Library/Logs/glasslab-exo-rdma-guard.log` when the device
  is missing.

## Logs And Recovery

Logs are retained outside `/tmp`:

```text
/Users/glasslab/Library/Logs/glasslab-exo.log
/Users/glasslab/Library/Logs/glasslab-exo.error.log
/Users/glasslab/Library/Logs/glasslab-exo-reconcile.log
/Users/glasslab/Library/Logs/glasslab-exo-reconcile.error.log
```

Restart a service without creating an unmanaged process:

```bash
sudo launchctl kickstart -k system/com.glasslab.exo
sudo launchctl kickstart -k system/com.glasslab.exo-reconcile  # .17 only
```

If the Thunderbolt interface is absent or inactive, repair that operating
system network state first. `launchd` deliberately waits rather than attempting
privileged network reconfiguration.

## Remaining Limitation

The LAN addresses `.17` and `.18` are currently DHCP leases from
`192.168.1.100`. Reserve their Ethernet MAC addresses on that DHCP server. Exo
itself communicates over the static Thunderbolt addresses, so a changed LAN
lease affects SSH aliases but not the internal model transport.
