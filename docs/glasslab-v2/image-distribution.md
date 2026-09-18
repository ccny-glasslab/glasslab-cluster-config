# Image Distribution

Glasslab's supported control-service image path is:

```text
merge to main
    |
    v
Publish Service Images
    |
    +-- workflow-api:<full-commit-sha>
    +-- research-orchestrator:<full-commit-sha>
    |
    v
GHCR
    |
    v
rollout-research-services.sh on .44
    |
    v
Kubernetes pulls the exact reviewed image
```

## Publishing

`.github/workflows/service-images.yml` runs when either control service or its
shared inputs change. It publishes both images as one release set:

- `glasslab-workflow-api:<full-commit-sha>`
- `glasslab-research-orchestrator:<full-commit-sha>`

Publishing both ensures the default rollout and rollback commands always have
a complete pair for the selected commit. Docker build caching keeps unchanged
dependency layers reusable.

Images use the full Git commit SHA. Mutable `latest` tags and manually chosen
version counters are not part of the deployment contract.

The manual Docker workflow remains available for diagnostics and explicit
rebuilds. It is not the normal release path.

## Deployment

Before deploying, run the read-only rollout-freeze check in
[`runbooks/rollout-freeze-preflight.md`](runbooks/rollout-freeze-preflight.md).
Never restart the orchestrator while a run is non-terminal.

The canonical `.44` checkout deploys an already-published commit:

```bash
cd /home/glasslab/cluster-config
./scripts/rollout-research-services.sh --sync
```

The script:

1. refuses a tracked dirty checkout
2. optionally fast-forwards to `origin/main`
3. uses the checked-out full commit SHA as the image tag
4. applies service configuration and policy manifests
5. atomically renders each Deployment with the selected image
6. waits for rollouts
7. runs workflow-api and orchestrator readiness checks
8. applies the conservative provisioner-local control-service image tag
   retention policy after those checks pass (the current and three newest tags
   per service are retained, along with images used by running containers)

Roll back by selecting a previously published commit:

```bash
./scripts/rollout-research-services.sh --tag <full-commit-sha>
```

## Credentials

- GitHub Actions has write access only to the packages it publishes.
- Kubernetes uses the namespace-local `glasslab-ghcr-pull` secret for read
  access.
- `.44` does not need a package-write token for a normal rollout.

## Legacy fallback

The local build, push, and containerd-import helpers remain break-glass tools
for registry outages. They are not the contributor or normal deployment path.
Node-local imports were useful during bring-up but make scheduling, rollback,
and provenance depend on hidden machine state.

## Local image retention

`rollout-research-services.sh` calls
`scripts/prune-control-service-images.sh --apply --retain-tag <deployed-sha>`
only after a successful rollout and smoke checks. The helper removes old Docker
*tags* for the two control-service repositories on `.44`; it never runs Docker
system prune, deletes containers or volumes, or touches unrelated images.

Preview cleanup without changing anything:

```bash
./scripts/prune-control-service-images.sh
```

Skip it during investigation with `--skip-image-prune` on the rollout command.
This policy manages the provisioner's Docker build/pull cache. Kubernetes
worker-node image eviction remains kubelet/containerd policy.
