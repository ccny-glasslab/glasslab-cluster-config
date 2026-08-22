#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_HOST="${REMOTE_HOST:-glasslab-provisioner}"
REMOTE_REPO="${REMOTE_REPO:-/home/glasslab/cluster-config}"
REMOTE_OUTPUT_DIR="${REMOTE_OUTPUT_DIR:-/home/glasslab/glasslab-secret-backups}"
REMOTE_VAULT_DIR="${GLASSLAB_SECRET_VAULT:-/home/glasslab/.local/share/glasslab-secrets}"
REMOTE_POLICY="${SOPS_CONFIG:-$REMOTE_REPO/.sops.yaml}"
LOCAL_OUTPUT_DIR="${LOCAL_OUTPUT_DIR:-$HOME/glasslab-secret-backups}"
STAMP="${STAMP:-$(date +%Y%m%d-%H%M%S)}"

usage() {
  cat <<'EOF'
Usage: pull-glasslab-secrets-backup.sh [--remote-host USER@HOST|ALIAS] [--remote-repo DIR] [--remote-output-dir DIR] [--local-output-dir DIR] [--vault-dir DIR] [--policy FILE] [--stamp STAMP]

Runs the encrypted-only inventory backup on the provisioner, pulls the tar.gz
archive and its SHA-256 sidecar into randomized private local staging, verifies
the download, and publishes both files without overwriting an existing backup.

Defaults:
  --remote-host        glasslab-provisioner
  --remote-repo        /home/glasslab/cluster-config
  --remote-output-dir  /home/glasslab/glasslab-secret-backups
  --local-output-dir   $HOME/glasslab-secret-backups
  --vault-dir          /home/glasslab/.local/share/glasslab-secrets
  --policy             <remote-repo>/.sops.yaml

No passphrase option exists: every payload document is already SOPS-encrypted.
EOF
}

policy_was_explicit=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --remote-host)
      REMOTE_HOST="$2"
      shift 2
      ;;
    --remote-repo)
      REMOTE_REPO="$2"
      shift 2
      ;;
    --remote-output-dir)
      REMOTE_OUTPUT_DIR="$2"
      shift 2
      ;;
    --local-output-dir)
      LOCAL_OUTPUT_DIR="$2"
      shift 2
      ;;
    --vault-dir)
      REMOTE_VAULT_DIR="$2"
      shift 2
      ;;
    --policy)
      REMOTE_POLICY="$2"
      policy_was_explicit=1
      shift 2
      ;;
    --stamp)
      STAMP="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ "$policy_was_explicit" -eq 0 && -z "${SOPS_CONFIG:-}" ]]; then
  REMOTE_POLICY="$REMOTE_REPO/.sops.yaml"
fi
if [[ ! "$REMOTE_HOST" =~ ^([A-Za-z0-9_.-]+@)?[A-Za-z0-9_.-]+$ ]] ||
  [[ "$REMOTE_HOST" == -* ]]; then
  printf 'Remote host must be a safe SSH alias or USER@HOST value.\n' >&2
  exit 1
fi
if [[ ! "$REMOTE_OUTPUT_DIR" =~ ^/[A-Za-z0-9_./-]+$ ]] ||
  [[ "$REMOTE_OUTPUT_DIR" == *"/../"* ]] || [[ "$REMOTE_OUTPUT_DIR" == *"/.." ]]; then
  printf 'Remote output directory must be an absolute normalized path.\n' >&2
  exit 1
fi
if [[ ! "$STAMP" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ ]]; then
  printf 'Backup stamp contains unsafe characters.\n' >&2
  exit 1
fi

archive_name="glasslab-secrets-${STAMP}.tar.gz"
checksum_name="${archive_name}.sha256"
mkdir -p -- "$LOCAL_OUTPUT_DIR"
chmod 700 -- "$LOCAL_OUTPUT_DIR"
if [[ -e "$LOCAL_OUTPUT_DIR/$archive_name" || -e "$LOCAL_OUTPUT_DIR/$checksum_name" ]]; then
  printf 'A local backup with this stamp already exists.\n' >&2
  exit 1
fi

shell_quote() {
  printf '%q' "$1"
}

remote_command="cd -- $(shell_quote "$REMOTE_REPO") && ./scripts/backup-glasslab-secrets.sh"
remote_command+=" --vault-dir $(shell_quote "$REMOTE_VAULT_DIR")"
remote_command+=" --policy $(shell_quote "$REMOTE_POLICY")"
remote_command+=" --output-dir $(shell_quote "$REMOTE_OUTPUT_DIR")"
remote_command+=" --stamp $(shell_quote "$STAMP")"

ssh -T -- "$REMOTE_HOST" "$remote_command"

pull_stage="$(mktemp -d -- "$LOCAL_OUTPUT_DIR/.glasslab-secret-pull.XXXXXXXX")"
cleanup() {
  rm -rf -- "$pull_stage"
}
trap cleanup EXIT HUP INT TERM
chmod 700 -- "$pull_stage"

scp -- \
  "${REMOTE_HOST}:${REMOTE_OUTPUT_DIR}/${archive_name}" \
  "${REMOTE_HOST}:${REMOTE_OUTPUT_DIR}/${checksum_name}" \
  "$pull_stage/"

python3 "$ROOT/scripts/secret_backup_restore.py" verify-archive \
  --archive "$pull_stage/$archive_name" \
  --checksum "$pull_stage/$checksum_name"

if ! ln -- "$pull_stage/$archive_name" "$LOCAL_OUTPUT_DIR/$archive_name"; then
  printf 'Could not publish the pulled archive without overwriting a file.\n' >&2
  exit 1
fi
if ! ln -- "$pull_stage/$checksum_name" "$LOCAL_OUTPUT_DIR/$checksum_name"; then
  rm -f -- "$LOCAL_OUTPUT_DIR/$archive_name"
  printf 'Could not publish the pulled checksum without overwriting a file.\n' >&2
  exit 1
fi

printf 'Pulled and verified encrypted-only backup artifacts into %s\n' "$LOCAL_OUTPUT_DIR"
