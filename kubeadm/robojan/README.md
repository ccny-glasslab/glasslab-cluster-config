# ROBOJAN on Glasslab

[ROBOJAN](https://github.com/ccny-glasslab/robojan) (Bob, Roz, Janet) is a
separate project that runs on Glasslab hardware. It lives in its own
namespace, `robojan`, and borrows two shared services from `glasslab-v2`:
Postgres (its own database and role) and MinIO (its own bucket and scoped
user). It does not touch the research orchestrator or workflow-api.

`scripts/deploy-glasslab-v2.sh` does not apply this directory. Apply it by
hand from the provisioner, as below. The only shared files it changes are the
Postgres and MinIO ingress policies, which admit Bob's pods and the one-shot
provisioner Jobs.

## Layout

| File | Namespace | What it is |
| --- | --- | --- |
| `00-namespace.yaml` | — | `robojan`, Pod Security `restricted` |
| `10-secret.example.yaml` | both | Secret contracts (documentation only) |
| `20-provision-postgres-job.yaml` | glasslab-v2 | Creates role and database `robojan_bob` |
| `30-minio-policy.yaml` | glasslab-v2 | Bucket policy: read and create in `robojan-originals`, no delete |
| `35-provision-minio-job.yaml` | glasslab-v2 | Creates the versioned bucket and Bob's scoped user |
| `40-bob-deployment.yaml` | robojan | Bob Deployment and ClusterIP Service on 8080 |
| `50-network-policy.yaml` | robojan | Bob accepts traffic only from the `robojan` namespace |

## First rollout (M1)

Run from the canonical checkout on the provisioner:

```bash
ssh glasslab-provisioner
cd /home/glasslab/cluster-config
export KUBECONFIG=/home/glasslab/.kube/config
```

1. Create the namespace and apply the updated shared ingress policies:

   ```bash
   kubectl apply -f kubeadm/robojan/00-namespace.yaml
   kubectl apply -f kubeadm/glasslab-v2/postgres/50-network-policy.yaml
   kubectl apply -f kubeadm/glasslab-v2/minio/50-network-policy.yaml
   ```

2. Create the four Secrets in `10-secret.example.yaml` through the SOPS
   operator boundary ([Restore Glasslab v2 secrets](../../docs/glasslab-v2/runbooks/restore-v2-secrets.md)).
   Generate a fresh random database password and MinIO secret key; never
   paste, echo, or commit them. Copy `glasslab-ghcr-pull` from `glasslab-v2`
   into `robojan`.

3. Provision the database and the bucket, and check the logs:

   ```bash
   kubectl apply -f kubeadm/robojan/20-provision-postgres-job.yaml
   kubectl apply -f kubeadm/robojan/30-minio-policy.yaml
   kubectl apply -f kubeadm/robojan/35-provision-minio-job.yaml
   kubectl -n glasslab-v2 wait --for=condition=complete --timeout=300s \
     job/robojan-bob-provision-postgres job/robojan-bob-provision-minio
   kubectl -n glasslab-v2 logs job/robojan-bob-provision-postgres
   kubectl -n glasslab-v2 logs job/robojan-bob-provision-minio
   ```

4. Pin Bob's image to the robojan `main` commit that CI built
   (`ghcr.io/ccny-glasslab/robojan-bob:<40-hex sha>`), then apply:

   ```bash
   kubectl apply -f kubeadm/robojan/40-bob-deployment.yaml
   kubectl apply -f kubeadm/robojan/50-network-policy.yaml
   kubectl -n robojan rollout status deploy/robojan-bob
   ```

   Bob creates its schema on start-up and refuses a non-UTF8 database.

## Using Bob

Bob has no ingress at M1. From a contributor workstation:

```bash
ssh -L 18090:127.0.0.1:18090 glasslab-provisioner \
  'sudo -n env KUBECONFIG=/home/glasslab/.kube/config \
   kubectl -n robojan port-forward svc/robojan-bob 18090:8080'
curl -fsS http://127.0.0.1:18090/healthz
curl -fsS -H "Authorization: Bearer $BOB_TOKEN" http://127.0.0.1:18090/manifest
```

The API is described in the robojan repository's `bob/README.md` and at
`/docs` on the running service.

## Rotation and removal

- Rotate the database password or MinIO key by updating the Secret(s), then
  deleting and re-applying the matching provisioner Job and restarting Bob.
- Originals are never deleted through Bob's credential. Removing ROBOJAN
  data is an operator decision made with the root MinIO and Postgres
  identities, not a routine step.
