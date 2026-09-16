# Provision MinIO scoped service users

Issue #243. The MinIO root credential used to be injected into workflow-api and
the GPU runner, so either pod could read, write, or delete every bucket. This
procedure provisions a bucket-scoped MinIO user for workflow-api and stops every
long-lived service from holding the root identity.

The root Secret `glasslab-v2-minio` is now read only by:

- the MinIO server itself (`kubeadm/glasslab-v2/minio/20-deployment.yaml`), and
- the one-shot provisioning Job
  (`kubeadm/glasslab-v2/minio/45-provision-scoped-users-job.yaml`).

The GPU runner no longer receives any MinIO credential.

## Why a one-shot Job

The provisioning step is a short, privileged, idempotent IAM convergence. A Job
manifest makes it reviewable, reproducible, re-runnable, and testable from the
committed tree without exposing the MinIO admin port or putting root credentials
in an operator shell history. It is a one-shot administrative pod, not a
long-lived service that processes agent-influenced input.

## Preconditions

- MinIO is running and the root Secret `glasslab-v2-minio` exists with keys
  `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD`.
- workflow-api is deployed. Its `GLASSLAB_WORKFLOW_API_MINIO_*` references are
  `optional: true`, so the pod starts whether or not the scoped Secret exists
  yet; the MinIO code path only reads them when
  `GLASSLAB_WORKFLOW_API_SOURCE_DOCUMENT_STORAGE_MODE=minio`.

## 1. Create the scoped Secret (never commit the value)

Create the Secret `glasslab-v2-workflow-api-minio` in `glasslab-v2` with exactly
these keys:

- `MINIO_ACCESS_KEY`
- `MINIO_SECRET_KEY`

Generate a unique, long, random `MINIO_SECRET_KEY` and store it through the
approved SOPS operator boundary described in
[Restore Glasslab v2 secrets](restore-v2-secrets.md). The encrypted vault is
external (`/home/glasslab/.local/share/glasslab-secrets`) and its live migration
is still deferred until SOPS enrollment completes; until then this step is
blocked and the scoped user cannot be provisioned.

Do not paste, echo, or commit the value. The tracked contract is
`kubeadm/glasslab-v2/workflow-api/10-secret.example`.

## 2. Apply the policy and the provisioning Job

Run from the canonical provisioner checkout:

```bash
ssh glasslab-provisioner
cd /home/glasslab/cluster-config
kubectl -n glasslab-v2 apply -f kubeadm/glasslab-v2/minio/40-scoped-user-policies.yaml
kubectl -n glasslab-v2 apply -f kubeadm/glasslab-v2/minio/45-provision-scoped-users-job.yaml
```

`scripts/deploy-glasslab-v2.sh` also applies both of these as part of the MinIO
directory; applying them explicitly is the targeted path.

## 3. Verify (metadata only, never values)

```bash
kubectl -n glasslab-v2 wait --for=condition=complete \
  job/glasslab-minio-provision-workflow-api --timeout=300s
kubectl -n glasslab-v2 logs job/glasslab-minio-provision-workflow-api
```

Expected log tail: `provisioned <access-key> with policy
glasslab-workflow-api-sources`. The Job script never prints either secret.

Confirm the Secret and its keys exist without decoding them:

```bash
kubectl -n glasslab-v2 get secret glasslab-v2-workflow-api-minio
```

## 4. Confirm the scope (optional)

The policy allows only the source-document bucket. To prove the boundary,
re-using the same digest-pinned `mc` image as the Job, confirm a list against a
different bucket is denied:

```bash
kubectl -n glasslab-v2 run minio-scope-check --rm -it --restart=Never \
  --image=quay.io/minio/mc@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727 \
  --env=MC_HOST_scoped="http://${ACCESS_KEY}:${SECRET_KEY}@glasslab-minio.glasslab-v2.svc.cluster.local:9000" \
  -- mc ls scoped/does-not-exist
```

An `Access Denied` result (rather than a bucket listing) is the expected
outcome. Building `MC_HOST_scoped` from environment keeps the credential out of
the process argv.

## Rotation

Rotation is the same operation as provisioning, because `mc admin user add`
updates an existing access key's secret in place:

1. Generate a new `MINIO_SECRET_KEY` and update the SOPS-managed
   `glasslab-v2-workflow-api-minio` Secret.
2. Re-run the Job so MinIO adopts the new value:

   ```bash
   kubectl -n glasslab-v2 delete job glasslab-minio-provision-workflow-api --ignore-not-found
   kubectl -n glasslab-v2 apply -f kubeadm/glasslab-v2/minio/45-provision-scoped-users-job.yaml
   kubectl -n glasslab-v2 wait --for=condition=complete \
     job/glasslab-minio-provision-workflow-api --timeout=300s
   ```

3. If `GLASSLAB_WORKFLOW_API_SOURCE_DOCUMENT_STORAGE_MODE=minio`, restart the
   deployment so the pod picks up the new Secret:

   ```bash
   kubectl -n glasslab-v2 rollout restart deployment/glasslab-workflow-api
   kubectl -n glasslab-v2 rollout status deployment/glasslab-workflow-api --timeout=300s
   ```

To add another bucket for workflow-api, extend
`kubeadm/glasslab-v2/minio/40-scoped-user-policies.yaml`, re-apply that
ConfigMap, and re-run the Job as above. Never widen the policy to `"*"`.

## Rollback

Setting `GLASSLAB_WORKFLOW_API_SOURCE_DOCUMENT_STORAGE_MODE=filesystem` removes
the only consumer of the scoped credential. Do not restore the root Secret into
workflow-api or the GPU runner; that re-opens issue #243.

## Residual risk

No RBAC change is required for this procedure. The workflow-api Role still cannot
read Secrets, and the provisioning Job sets `automountServiceAccountToken:
false`, so neither holds API authority over the root Secret.

One namespace-level risk is unchanged and out of scope here: the MinIO root
Secret lives in `glasslab-v2`, and any identity that can create pods in that
namespace can mount it through the kubelet regardless of Secret RBAC. Closing
that requires isolating the root Secret from the workload namespace (for example
a dedicated MinIO namespace), which is a separate architecture change.
