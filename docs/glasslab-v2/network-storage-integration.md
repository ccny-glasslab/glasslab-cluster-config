# Network Storage Integration

This note maps shared network storage onto the services that are actually live in Glasslab now.

It is intentionally pragmatic.

## Why This Matters Now

As of the 2026-03-19 live validation:

- `glasslab-v2` core services are running
- OpenClaw is running
- WhatsApp is active
- `vLLM` is running
- the platform is now far enough along that storage work is no longer abstract future planning

Reference:

- `../live-state-2026-03-19.md`

## The Current Live Placement

- `workflow-api` on `node03`
- `Postgres` on `node01`
- `MinIO` on `node01`
- `OpenClaw` on `node01`
- `NATS` on `node05`

As of 2026-03-23, the lab also has a validated NFS target:

- server: `192.168.1.207`
- export root tested: `/volume1/backup`
- worker validation confirmed from `node01` and `node03`

This matters because any network storage plan should be evaluated against what is already pinned, already local, and already functioning.

## NAS Exports (`g-nas`, `192.168.1.207`)

The NAS is a Synology DiskStation (DSM web UI `https://192.168.1.207:5001`).
Its shares and NFS rules live in DSM, not in this repository, so this table is
the record. Credentials are not stored here; changing a share needs a DSM
administrator login.

`showmount -e 192.168.1.207` from the provisioner, 2026-10-20:

| Share | Export path | NFS clients | Used by |
|---|---|---|---|
| `backup` | `/volume1/backup` | `192.168.1.0/24` | `glasslab-v2` static PVs ([`20-nfs-static-pv.yaml`](../../kubeadm/glasslab-v2/storage/20-nfs-static-pv.yaml)) |
| `glasslab-jupyter` | `/volume1/glasslab-jupyter` | `192.168.1.0/24` | JupyterHub user storage (exams, researcher hubs). Created 2026-10-20 |
| `home1` | `/volume1/home1` | `192.168.1.0/24`, `134.74.112.15`, `134.74.146.39` | Not used by the cluster |

### `glasslab-jupyter`

NFS rule (DSM: Shared Folder → Edit → NFS Permissions), entered by a DSM
administrator on 2026-10-20. The client scope is confirmed by `showmount`, and
Read/Write and "No mapping" by the probe below; the other boxes were not read
back from DSM:

- Hostname or IP: `192.168.1.0/24`, the same scope as `backup`, so a new
  worker needs no new rule
- Privilege: Read/Write
- Squash: **No mapping**
- Security: `sys`
- Allow connections from non-privileged ports: off
- Allow users to access mounted subfolders: on
- Recycle Bin and encryption: off
- Asynchronous: not recorded; check it in DSM when the share is next opened

Share root on the NAS: owner `0:0`, mode **`0755`, plain Unix permissions**.

Why "No mapping": JupyterHub runs every user's server as UID 1000, and
per-user directories are owned by `1000:1000` with mode `0700`. The NAS has to
see the client's real UID so that ownership and mode bits mean something. With
squashing, every user would map to one NAS account, and the users could read
each other's directories.

Why the root has plain Unix permissions: DSM creates a share with Synology
ACLs. Over NFS those directories list as `0777`, but the ACL only grants
DSM accounts, so a client UID that is not a DSM user (like 1000) is refused
even `ls` on the share root. "No mapping" alone did not fix that. A root pod
ran `chmod 0755` on the share root over NFS, which replaced the ACL with
ordinary mode bits; directories created under it afterwards are plain Unix
too. **Do not edit this share's permissions in DSM (Shared Folder →
Permissions, or File Station → Properties → Permission)**: that can put the
ACL back and lock non-root users out again. If it happens, repeat the
`chmod 0755` from a root pod.

Root is not squashed, so pods that run as root can read and change everything
in the share. Only the JupyterHub preparation Jobs should run as root against
it; user pods run as UID 1000 with no capabilities.

Verified 2026-10-20 from `node01` with a throwaway PV
(`nfsvers=4.1`, `Retain`, pre-bound PVC) and the `exam-student:release` image:

- root: `mkdir users/_probe`, `chown 1000:1000`, `chmod 0700`
- UID 1000: wrote, read and listed a file and created a subdirectory there;
  `ls -ln` shows owner `1000`
- UID 1001: list, read and write of `users/_probe` all denied
- the probe PV, PVC, pods and directory were removed afterwards; the share
  probe directories are gone

### `backup`

Its NFS rule was not changed and has not been read from DSM. From the client
side (2026-10-20): root is not squashed; UID 1000 cannot write even in a
directory that lists as `0777`, over NFSv4.1 and NFSv3. That matches the
Synology ACL behaviour above rather than a squash rule, but it was not
re-tested on `backup`. Changing modes there would affect the existing
`glasslab-v2` PVs, so leave it as it is unless a non-root workload needs it.

## What Network Storage Can Help With

Shared network storage can help Glasslab by:

- reducing node-local friction for datasets
- making artifact access less tied to one node
- giving a simpler first answer for shared state before adopting a fuller CSI-backed platform
- lowering the operational pain of service relocation for some workloads

It does not automatically solve:

- image distribution
- tool-calling reliability
- secret handling
- all database durability concerns

## Recommended Fit By Service

### Datasets

Strong candidate for early network storage.

Why:

- datasets are often read-heavy
- multiple services may want to see the same content
- this reduces repeated local staging

Best first use:

- future workflow input corpora
- literature or replication input bundles

Current tracked path:

- `/volume1/backup/glasslab-v2/shared-datasets`

### Artifacts

Strong candidate for early network storage.

Why:

- artifacts are often read after the run completes
- shared access is useful for evaluator, reporter, and operator-facing review
- this reduces node-specific collection pain

Possible pattern:

- keep execution local
- publish artifacts to MinIO
- optionally stage or mirror shared artifact areas on network storage during the transition

Current tracked path:

- `/volume1/backup/glasslab-v2/shared-artifacts`

### OpenClaw State

Later candidate, no longer first priority.

Why:

- current OpenClaw state is already on retained local PV/PVC storage on `node01`
- that local step already removed the most immediate fragility around WhatsApp/device/session state

Caution:

- only move it again after deciding whether the operational goal is “shared failover-capable gateway state” or simply “good-enough local durability until NFS exists”

### Postgres

Possible candidate, but not recommended as the first casual network-storage move.

Why:

- databases are sensitive to latency and correctness behavior
- local durable storage is easier to reason about first

Preferred order:

- first make Postgres durable on explicit local storage
- only consider network-backed Postgres after the storage path is deliberate and tested

### MinIO

Possible candidate, but also not the first casual move.

Why:

- MinIO benefits from durable, well-understood storage more than from “shared because it is available”

Better first question:

- do we want MinIO to stay node-pinned but durable, or become network-backed immediately?

### NATS

Low priority for network storage unless JetStream durability becomes materially important.

Why:

- the current role does not appear to demand the same storage priority as Postgres and MinIO

### vLLM Cache

Poor early candidate for network storage.

Why:

- model cache is large and performance-sensitive
- local storage is usually the simpler and better first answer

## Conservative Rollout Order

If network storage is coming online now, the safest rollout order is:

1. datasets
2. shared artifact access
3. optional OpenClaw state
4. only later: evaluate whether MinIO or Postgres should consume it directly

This avoids risking the most sensitive state first.

## Recommended Integration Pattern

The practical staged pattern is:

1. bring network storage online
2. mount it for low-risk shared data first
3. validate performance and operational behavior
4. decide whether it remains a shared data tier only or becomes part of the durable core-service plan

Short version:

- use network storage first for sharing
- use local explicit storage first for sensitive state

## What To Decide Before Wiring It In

1. Is the new network storage meant for sharing, durability, or both?
2. Is it NFS-like shared storage, or something with stronger local semantics?
3. Which node or machine is backing it?
4. Is it intended to survive node loss, `.44` loss, or just reduce node pinning?
5. What workloads are acceptable to move first without risking the current live platform?

## Glasslab-Specific Recommendation

Given the current live state, the best immediate use of new network storage would probably be:

- shared datasets
- shared artifact review paths

That is now the committed first integration step:

- explicit RWX NFS PV/PVCs for datasets and artifacts, now sized to reserve `5Ti` total
- no immediate cutover of Postgres, MinIO, or NATS

The best immediate use would probably not be:

- moving Postgres first
- moving MinIO first
- moving the `vLLM` cache first

If the network storage proves stable and boring, the next candidate worth reconsidering is:

- durable OpenClaw state

That would directly improve the current live operator path, especially the chat-channel lifecycle.
