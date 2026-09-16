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
