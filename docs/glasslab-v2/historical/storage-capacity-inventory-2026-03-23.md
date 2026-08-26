# Storage Capacity Inventory

Date: 2026-03-23

This inventory records what was actually observed from the lab during the NFS bring-up and follow-on storage work.

It is a capacity snapshot, not a promise that every byte is safely schedulable for Kubernetes.

## Shared NFS Storage

Observed from `.44` after mounting `192.168.1.207:/volume1/backup`:

- export: `192.168.1.207:/volume1/backup`
- total size: about `11T`
- used: about `1.9T`
- free: about `9.0T`
- current Glasslab footprint under `/volume1/backup/glasslab-v2`: about `20K`

Important interpretation:

- the earlier `100Gi + 100Gi` PV sizes were only Kubernetes claims
- they were not the actual capacity of the NFS server
- the real shared pool available to Glasslab is much larger than the initial `5Ti` reservation

## Kubernetes NFS Claims

Current tracked shared claims:

- `glasslab-shared-datasets`: `2Ti`
- `glasslab-shared-artifacts`: `3Ti`

These should be treated as conservative first allocations, not the storage ceiling.

## Local Node Storage Snapshot

Observed root-backed local storage on the cluster nodes:

- `cp01`
  - device: single `465.8G` disk
  - mounted root capacity: about `458G`
  - free: about `421G`
- `node01`
  - devices: `2 x 465.8G` in `md0`
  - mounted root capacity: about `458G`
  - free: about `414G`
- `node02`
  - device: single `465.8G` disk
  - mounted root capacity: about `458G`
  - free: about `379G`
- `node03`
  - device: single `465.8G` disk
  - mounted root capacity: about `458G`
  - free: about `420G`
- `node04`
  - device: single `931.5G` disk
  - mounted root capacity: about `916G`
  - free: about `855G`
- `node05`
  - device: single `119.2G` disk
  - mounted root capacity: about `117G`
  - free: about `95G`

## Practical Reading Of The Numbers

The lab now has two different storage pools:

- a large shared NFS pool with roughly `9T` free
- several smaller node-local pools with different performance and failure characteristics

These pools should not be treated as interchangeable.

### Shared NFS pool

Best for:

- shared datasets
- shared artifacts
- paths that need to survive pod movement between nodes

### Local node pools

Best for:

- low-latency single-node state
- GPU-adjacent caches
- services where simple local semantics are preferable to casual network storage

## Current Glasslab v2 Placement

Local durable state currently lives on:

- `node01`: Postgres, MinIO, OpenClaw state
- `node05`: NATS JetStream state

Shared storage currently lives on:

- `192.168.1.207:/volume1/backup/glasslab-v2/shared-datasets`
- `192.168.1.207:/volume1/backup/glasslab-v2/shared-artifacts`

## What This Means For Planning

- there is much more shared storage available than the current PV sizes suggest
- node-local storage is still useful and substantial, especially on `node04`
- node-loss tolerance will come from deciding which services should move from local PVs toward shared or replicated storage, not from pretending all capacity belongs to one homogeneous pool
