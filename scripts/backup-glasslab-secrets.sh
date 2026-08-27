#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VAULT_DIR="${GLASSLAB_SECRET_VAULT:-/home/glasslab/.local/share/glasslab-secrets}"
POLICY="${SOPS_CONFIG:-$ROOT/.sops.yaml}"
OUTPUT_DIR="${OUTPUT_DIR:-$HOME/glasslab-secret-backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"

usage() {
  cat <<'EOF'
Usage: backup-glasslab-secrets.sh [--vault-dir DIR] [--policy FILE] [--output-dir DIR] [--stamp STAMP]

Creates a tar.gz recovery bundle containing only already-encrypted *.sops.yaml
documents, vault/inventory.yaml, the public .sops.yaml policy, and SHA-256
checksums. The helper never invokes sops decryption or creates a plaintext tar.

Defaults:
  --vault-dir   $GLASSLAB_SECRET_VAULT or /home/glasslab/.local/share/glasslab-secrets
  --policy      $SOPS_CONFIG or the repository .sops.yaml
  --output-dir  $OUTPUT_DIR or $HOME/glasslab-secret-backups
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vault-dir)
      VAULT_DIR="$2"
      shift 2
      ;;
    --policy)
      POLICY="$2"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="$2"
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

if [[ ! "$STAMP" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ ]]; then
  printf 'Backup stamp contains unsafe characters.\n' >&2
  exit 1
fi

ARCHIVE_PATH="$OUTPUT_DIR/glasslab-secrets-${STAMP}.tar.gz"

python3 "$ROOT/scripts/secret_backup_restore.py" backup \
  --vault-dir "$VAULT_DIR" \
  --policy "$POLICY" \
  --output "$ARCHIVE_PATH"
