#!/usr/bin/env bash
# Fail-closed preflight: the checkout a rollout applies must not be writable by
# anyone but its owner (issue #599).
#
# deploy-glasslab-v2.sh and rollout-research-services.sh apply manifests and
# run scripts from this tree as a cluster admin. The canonical checkout's group
# (glasslab) includes every contributor account, so a group-writable manifest,
# script, or .git/ entry (hooks, config) lets any group member substitute what
# the next human rollout applies. Refuse when any path is other-writable, or
# group-writable through a shared group.
#
# A group is "shared" unless it is the owner's user-private group (same name as
# the owning user, no listed members): umask 002 checkouts on a workstation are
# group-writable only to their owner and pass. --strict-group treats every
# group as shared.
#
# Read-only: never modifies a file or prints file contents.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: check-checkout-permissions.sh [--strict-group] <directory>

Exit 0 when no path under <directory> (including .git/) is other-writable or
group-writable through a shared group. Exit 1 and list the offenders (first 20)
otherwise. Symlinks are skipped; their own mode bits are not used.
USAGE
}

strict_group=false
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
if [[ "${1:-}" == "--strict-group" ]]; then
  strict_group=true
  shift
fi
if [[ $# -ne 1 || ! -d "$1" ]]; then
  usage >&2
  exit 2
fi
root="$1"

declare -A shared_cache=()
group_is_shared() {
  local owner="$1" group="$2"
  if [[ "$strict_group" == true ]]; then
    return 0
  fi
  if [[ -z "${shared_cache[$group]+set}" ]]; then
    local members
    members="$(getent group "$group" 2>/dev/null | cut -d: -f4 || true)"
    if [[ "$group" == "$owner" && -z "$members" ]]; then
      shared_cache[$group]=no
    else
      shared_cache[$group]=yes
    fi
  fi
  [[ "${shared_cache[$group]}" == yes ]]
}

offenders=0
while IFS= read -r -d '' path; do
  if ! read -r mode owner group < <(stat -c '%a %U %G' -- "$path" 2>/dev/null); then
    printf '[check-checkout-permissions] refusing: cannot stat %s\n' "$path" >&2
    offenders=$((offenders + 1))
    continue
  fi
  reason=''
  if (( (8#$mode & 8#002) != 0 )); then
    reason='other-writable'
  elif (( (8#$mode & 8#020) != 0 )) && group_is_shared "$owner" "$group"; then
    reason="group-writable by shared group $group"
  fi
  [[ -n "$reason" ]] || continue
  offenders=$((offenders + 1))
  if (( offenders <= 20 )); then
    printf '[check-checkout-permissions] refusing: %s is mode %s (%s)\n' \
      "$path" "$mode" "$reason" >&2
  fi
done < <(find "$root" ! -type l -perm /022 -print0 2>/dev/null)

if (( offenders > 0 )); then
  printf '[check-checkout-permissions] %d path(s) under %s are writable beyond their owner; as the owner run: chmod -R go-w %q\n' \
    "$offenders" "$root" "$root" >&2
  exit 1
fi
