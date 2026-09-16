# MinIO

MinIO manifests for v2 object storage.

## Layout

- `10-secret.example.yaml` — documentation contract for the MinIO ROOT identity
  (`glasslab-v2-minio`). Only the MinIO server and the one-shot provisioning Job
  read it.
- `20-deployment.yaml` — the MinIO server.
- `30-service.yaml`, `50-network-policy.yaml` — cluster access.
- `40-scoped-user-policies.yaml` — bucket-scoped IAM policy documents.
- `45-provision-scoped-users-job.yaml` — one-shot, idempotent Job that creates
  the workflow-api scoped user and attaches the policy.

## Credential model

Long-lived services do not receive the root credential. workflow-api reads a
bucket-scoped user from its own Secret (`glasslab-v2-workflow-api-minio`); the
GPU runner receives no MinIO credential at all. See
[Provision MinIO scoped users](../../../docs/glasslab-v2/runbooks/provision-minio-scoped-users.md)
for the provisioning and rotation procedure.
