# Workflow API

Deployment manifests for the v2 `workflow-api` service live here.

Do not apply this directory wholesale as part of a live rollout. The
`10-secret.example` file is documentation only; the live DSN secret is an
ignored `.44`-local file at:

```text
kubeadm/glasslab-v2/secrets/15-workflow-api.local.yaml
```

The `Publish Service Images` workflow builds the image on merges to `main`.
Use the guarded rollout helper from the canonical `.44` checkout after that
workflow succeeds:

```bash
cd /home/glasslab/cluster-config
./scripts/rollout-research-services.sh --sync --service workflow-api
```

Current prerequisite boundary for execution preflight and run submission:

- shared datasets PVC: `glasslab-shared-datasets`
- shared artifacts PVC: `glasslab-shared-artifacts`
- image pull secret: `glasslab-ghcr-pull`
- service account: `glasslab-workflow-api`

The service account needs:

- namespace-scoped read access to PVCs in `glasslab-v2`
- namespace-scoped `get` on the single named image pull secret
  `glasslab-ghcr-pull` (resourceNames-scoped, so no other secret is readable)
- cluster-scoped read access to:
  - `nodes`
  - `pods`

Those reads are required because execution preflight checks:

- whether the dataset and artifacts PVCs exist and are `Bound`
- whether the image pull secret exists
- whether any `Ready` nodes currently satisfy the requested resource profile
- current pod allocations when estimating node fit

If these prerequisites are missing, the operator path will surface infrastructure blockers before a run is accepted.
