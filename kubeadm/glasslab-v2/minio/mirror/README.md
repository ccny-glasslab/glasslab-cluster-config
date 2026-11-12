# MinIO mirror images

MinIO's registries no longer serve anonymous pulls. On 2026-11-12, from the
provisioner:

- `quay.io/api/v1/repository/minio/mc` and `.../minio/minio` returned `401`
  while another public quay repository returned `200`;
- a scoped quay pull token was still rejected on the manifest;
- `hub.docker.com/v2/repositories/minio/mc` and `.../minio/minio` returned `404`;
- `dl.min.io/client/mc/...` returned `410`.

The cluster already ran `docker.io/minio/minio@sha256:14cea493…`, but a fresh
node could not pull it, so any reschedule or reinstall would break the shared
object store. These images are that running release, rebuilt from the binaries
inside the live pod and published to GHCR, which the cluster already pulls from
with `glasslab-ghcr-pull`.

| Image | Contents | Source |
| --- | --- | --- |
| `ghcr.io/ccny-glasslab/minio-mirror@sha256:acc08df7821500b6fae33d8eabe6dd0fbf9927d819ec5069de1d097ba4b643db` | MinIO server `RELEASE.2025-09-07T16-13-09Z` | `docker.io/minio/minio@sha256:14cea493…` |
| `ghcr.io/ccny-glasslab/minio-mirror@sha256:f95c54518ddcc0f1e01763e3e26add98af501f0faf87e5da6686229569ff6ee9` | `mc` `RELEASE.2025-08-13T08-35-41Z` | same image |

Both are `alpine:3.20` plus the static binary and the CA bundle from the pod, so
`sh` and `kubectl exec` keep working. The OCI labels record the source image,
the release, and the binary SHA-256.

## Rebuild

Run on the provisioner while a `glasslab-minio` pod is running:

```bash
GHCR_TOKEN="$(gh auth token)" ./build.sh --push
```

The script extracts `/usr/bin/minio`, `/usr/bin/mc`, and
`/etc/ssl/certs/ca-certificates.crt` from `deploy/glasslab-minio`, builds both
images, checks that the binary versions match the labels below, and optionally
pushes. Without a token it only builds and prints the digests.

Update this table and the three manifests (`20-deployment.yaml`,
`45-provision-scoped-users-job.yaml`, `kubeadm/robojan/35-provision-minio-job.yaml`)
when the digests change, and rebuild before a MinIO upgrade rather than pulling
from MinIO's registries.
