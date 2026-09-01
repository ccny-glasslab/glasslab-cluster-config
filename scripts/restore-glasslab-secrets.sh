#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VAULT_DIR="${GLASSLAB_SECRET_VAULT:-/home/glasslab/.local/share/glasslab-secrets}"
ARCHIVE=""
CHECKSUM=""
CONFIRM=0

usage() {
  cat <<'EOF'
Usage: restore-glasslab-secrets.sh --archive FILE [--checksum FILE] [--vault-dir DIR] --yes

Preflights archive paths and types in Python, extracts with tar --no-same-owner
into a randomized mode-0700 sibling directory, verifies inventory coverage,
SHA-256 checksums, policy, and SOPS metadata, then atomically replaces the vault.
An existing vault is preserved as a dated rollback directory.

The --yes flag is required before any validated staging vault can replace the
active vault. This command never decrypts or prints a secret document.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --archive)
      ARCHIVE="$2"
      shift 2
      ;;
    --checksum)
      CHECKSUM="$2"
      shift 2
      ;;
    --vault-dir)
      VAULT_DIR="$2"
      shift 2
      ;;
    --yes)
      CONFIRM=1
      shift
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

if [[ -z "$ARCHIVE" ]]; then
  printf '%s\n' '--archive is required.' >&2
  usage >&2
  exit 1
fi

command=(
  python3 "$ROOT/scripts/secret_backup_restore.py" restore
  --archive "$ARCHIVE"
  --vault-dir "$VAULT_DIR"
)
if [[ -n "$CHECKSUM" ]]; then
  command+=(--checksum "$CHECKSUM")
fi
if [[ "$CONFIRM" -eq 1 ]]; then
  command+=(--yes)
fi
exec "${command[@]}"
