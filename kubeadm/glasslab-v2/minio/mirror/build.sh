#!/usr/bin/env bash
# Rebuild the MinIO mirror images from the running glasslab-minio pod.
set -euo pipefail

namespace=${NAMESPACE:-glasslab-v2}
deployment=${DEPLOYMENT:-glasslab-minio}
registry=${REGISTRY:-ghcr.io/ccny-glasslab/minio-mirror}
mc_version=RELEASE.2025-08-13T08-35-41Z
minio_version=RELEASE.2025-09-07T16-13-09Z
push=false
[[ "${1:-}" == "--push" ]] && push=true

[[ $# -le 1 ]] || { echo "usage: $0 [--push]" >&2; exit 1; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

extract() {
  kubectl -n "$namespace" exec "deploy/$deployment" -- cat "$1"
}

extract /usr/bin/mc >"$work/mc"
extract /usr/bin/minio >"$work/minio"
extract /etc/ssl/certs/ca-certificates.crt >"$work/ca-certificates.crt"
chmod 755 "$work/mc" "$work/minio"

write_dockerfile() {
  local path=$1 title=$2 version=$3 binary=$4 sha256=$5 entrypoint=$6
  cat >"$path" <<EOF
FROM alpine:3.20

LABEL org.opencontainers.image.title="$title" \\
      org.opencontainers.image.version="$version" \\
      org.opencontainers.image.source="https://github.com/ccny-glasslab/glasslab-cluster-config" \\
      glasslab.io/mirrored-from="docker.io/minio/minio@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e" \\
      glasslab.io/binary-sha256="$sha256"

COPY ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY $binary /usr/bin/$binary
ENTRYPOINT ["$entrypoint"]
EOF
}

mc_sha=$(sha256sum "$work/mc" | cut -d' ' -f1)
minio_sha=$(sha256sum "$work/minio" | cut -d' ' -f1)
write_dockerfile "$work/Dockerfile.mc" "MinIO client mirror" "$mc_version" mc "$mc_sha" /usr/bin/mc
write_dockerfile "$work/Dockerfile.minio" "MinIO server mirror" "$minio_version" minio "$minio_sha" /usr/bin/minio
printf 'EXPOSE 9000 9001\n' >>"$work/Dockerfile.minio"

docker build -f "$work/Dockerfile.mc" -t "$registry:mc-$mc_version" "$work"
docker build -f "$work/Dockerfile.minio" -t "$registry:minio-$minio_version" "$work"

docker run --rm "$registry:mc-$mc_version" --version | grep -q "$mc_version" \
  || { echo "built mc does not report $mc_version" >&2; exit 1; }
docker run --rm "$registry:minio-$minio_version" --version | grep -q "$minio_version" \
  || { echo "built minio does not report $minio_version" >&2; exit 1; }

if $push; then
  : "${GHCR_TOKEN:?set GHCR_TOKEN to a token with write:packages}"
  printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "${GHCR_USER:-OffensiveGeneric}" --password-stdin >/dev/null
  docker push "$registry:mc-$mc_version"
  docker push "$registry:minio-$minio_version"
  docker logout ghcr.io >/dev/null
fi

docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' \
  "$registry:mc-$mc_version" "$registry:minio-$minio_version"
