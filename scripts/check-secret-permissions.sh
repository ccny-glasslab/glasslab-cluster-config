#!/usr/bin/env bash
# Fail-closed preflight for live *.local.yaml Secret manifests.
#
# The live manifests in kubeadm/glasslab-v2/secrets/ carry database DSNs and
# object-store keys. The canonical checkout is intentionally group-traversable
# (0755), and its group (glasslab) includes every contributor account, so any
# group/other permission bit on a *.local.yaml exposes or lets someone rewrite
# live credentials. Refuse when a manifest is readable or writable by anyone
# other than its owner; owner-only (0600) is the enforced contract.
#
# Read-only: never modifies a file or prints file contents.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: check-secret-permissions.sh <directory> [<directory>...]

Exit 0 when every *.local.yaml in each directory is owner-only (no group or
other permission bits). Exit 1 and print the offending path and mode otherwise.
Nonexistent directories are skipped.
USAGE
}

if [[ $# -eq 0 ]]; then
  usage >&2
  exit 2
fi

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

status=0
offenders=0
for dir in "$@"; do
  [[ -d "$dir" ]] || continue
  while IFS= read -r -d '' file; do
    if ! mode="$(stat -Lc '%a' -- "$file" 2>/dev/null)"; then
      printf '[check-secret-permissions] refusing: cannot stat %s\n' "$file" >&2
      status=1
      offenders=$((offenders + 1))
      continue
    fi
    if (( (8#$mode & 8#077) != 0 )); then
      printf '[check-secret-permissions] refusing: %s is mode %s; live Secret manifests must be owner-only (0600)\n' \
        "$file" "$mode" >&2
      status=1
      offenders=$((offenders + 1))
    fi
  done < <(find "$dir" -maxdepth 1 \( -type f -o -type l \) -name '*.local.yaml' -print0 2>/dev/null)
done

if (( status != 0 )); then
  printf '[check-secret-permissions] %d local Secret manifest(s) with group/other access; fix with: chmod 600 <path>\n' \
    "$offenders" >&2
fi
exit "$status"
