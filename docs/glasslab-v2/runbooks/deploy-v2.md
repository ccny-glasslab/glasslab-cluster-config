# Deploy Glasslab v2

1. Log into the provisioner host and move to the canonical repo.

```bash
ssh glasslab@192.168.1.44
cd /home/glasslab/cluster-config
```

2. Review the example secret manifests before applying anything.

```bash
sed -n '1,200p' kubeadm/glasslab-v2/postgres/10-secret.example.yaml
sed -n '1,200p' kubeadm/glasslab-v2/minio/10-secret.example.yaml
```

3. Create local non-committed core secret manifests under `kubeadm/glasslab-v2/secrets/`.

Recommended files:
- `kubeadm/glasslab-v2/secrets/10-postgres.local.yaml`
- `kubeadm/glasslab-v2/secrets/20-minio.local.yaml`

Back them up separately. Git and `scripts/snapshot-provisioner-config.sh` do not capture these files.

4. Validate the workflow registry definitions.

```bash
./scripts/seed-registry.sh
```

5. Confirm that the `Publish Service Images` workflow succeeded for the commit
being deployed. It publishes `workflow-api` and `research-orchestrator` images
under the full Git commit SHA.

```bash
git rev-parse HEAD
gh run list --workflow "Publish Service Images" --limit 5
```

Current assumptions:
- the `glasslab-v2` namespace contains a `glasslab-ghcr-pull` Docker registry secret
- the shared PVCs `glasslab-shared-datasets` and `glasslab-shared-artifacts` exist and are `Bound`
- control-service images are pullable by full commit SHA
- the bounded-agent Deployments pull:
  - `ghcr.io/ccny-glasslab/glasslab-intake-agent:0.1.0`
  - `ghcr.io/ccny-glasslab/glasslab-interpretation-agent:0.1.0`
  - `ghcr.io/ccny-glasslab/glasslab-assessment-agent:0.1.0`
  - `ghcr.io/ccny-glasslab/glasslab-design-agent:0.1.0`
  - `ghcr.io/ccny-glasslab/glasslab-schedule-worker:0.1.0`
- local push/import helpers are break-glass fallback paths

Prereq check:

```bash
./scripts/check-v2-run-prereqs.sh
```

6. For initial infrastructure creation, apply the v2 core manifest tree.

```bash
./scripts/deploy-glasslab-v2.sh
```

The deploy script applies local files in `kubeadm/glasslab-v2/secrets/` and skips any `*.example.yaml` manifests.

It also applies the first explicit scheduling lanes:

- `glasslab-user-high`
- `glasslab-autonomous-low`

Current storage caveat:
- Postgres and MinIO are still non-durable until the storage plan under `docs/glasslab-v2/storage-and-state.md` is implemented

7. Roll out the CI-published control services. For routine updates this is the
only deployment command required:

```bash
./scripts/rollout-research-services.sh --sync
```

Deploy one service with `--service workflow-api` or
`--service research-orchestrator`. Roll back with
`--tag <previous-full-commit-sha>`.

8. Verify rollout state and health endpoints.

```bash
./scripts/smoke-test-v2.sh
./scripts/smoke-test-v2.sh --include-bounded-agents
./scripts/check-live-provenance.sh
```

The provenance check should make drift obvious:

- `workflow-api` should report the expected `build_source_revision` and `build_source_label`
- if provenance is missing, the rollout is incomplete even if pods are `Running`

Bounded-agent rollout check:

```bash
kubectl -n glasslab-v2 rollout status deployment/glasslab-intake-agent --timeout=120s
kubectl -n glasslab-v2 rollout status deployment/glasslab-interpretation-agent --timeout=120s
kubectl -n glasslab-v2 rollout status deployment/glasslab-assessment-agent --timeout=120s
kubectl -n glasslab-v2 rollout status deployment/glasslab-design-agent --timeout=120s
kubectl -n glasslab-v2 rollout status deployment/glasslab-schedule-worker --timeout=120s
```

9. If the smoke test fails, inspect the namespace directly.

```bash
kubectl -n glasslab-v2 get pods -o wide
kubectl -n glasslab-v2 describe deploy/glasslab-workflow-api
kubectl -n glasslab-v2 describe statefulset/glasslab-postgres
kubectl -n glasslab-v2 logs deploy/glasslab-workflow-api --tail=200
```

10. If the rollout fails with image pull errors, verify the private registry secret before falling back to a manual import.

```bash
kubectl -n glasslab-v2 get secret glasslab-ghcr-pull
kubectl -n glasslab-v2 describe pod -l app.kubernetes.io/name=glasslab-workflow-api
```

11. Do not expose v2 publicly yet. Keep access internal until the command surface and backend policies are fully validated.

12. Keep all bounded-agent feature flags disabled in `workflow-api` until each service has been deployed and tested one stage at a time.

## Rehearsal resume (shared PVC)

The real-model rehearsal driver (`app.rehearse_research_flow`, in the
orchestrator image) keeps its checkpoint (`checkpoint.json`) and SQLite store
under `REHEARSE_ROOT`. The deployment pins that path to the shared artifacts
PVC:

```text
REHEARSE_ROOT=/mnt/artifacts/research-orchestrator/rehearsal
```

Because this is on `glasslab-shared-artifacts` rather than the pod's
`emptyDir` `/tmp`, a `Recreate` rollout no longer destroys rehearsal state.
The running process is still killed by a rollout; relaunch it to resume from
the durable checkpoint.

The driver has no `--resume` flag and needs none: it is state-driven. On a
relaunch it reloads `checkpoint.json`, calls `engine.recover()` for any
already-approved gate, and resumes a paused run via `engine.resume_run()`.
To continue a rehearsal, exec the driver again:

```bash
kubectl -n glasslab-v2 exec deploy/glasslab-research-orchestrator -- \
  python3 -m app.rehearse_research_flow
```

A `RESUMABLE` result exits 0 and is a clean stop (wall-clock turn timeout);
relaunch the same command to continue. A `FAIL` result exits 1. Inspect the
durable state directly with:

```bash
kubectl -n glasslab-v2 exec deploy/glasslab-research-orchestrator -- \
  ls -la /mnt/artifacts/research-orchestrator/rehearsal
```

Reference:

- `runbooks/deploy-bounded-agents.md`
