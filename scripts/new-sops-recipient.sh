#!/usr/bin/env bash
# Create one operator's passphrase-protected OpenPGP key and print only the
# public fingerprint. Run this in your own account; never share the private key.
set -euo pipefail

identity=${1:-}
if [[ -z "$identity" ]]; then
  echo "usage: $0 'Full Name <email>'" >&2
  exit 1
fi

if gpg --list-secret-keys --with-colons "$identity" 2>/dev/null | grep -q '^sec'; then
  echo "a secret key already exists for $identity; reusing it" >&2
else
  gpg --quick-generate-key "$identity" default default 2y
fi

fingerprint=$(gpg --list-keys --with-colons "$identity" | awk -F: '/^fpr/ {print $10; exit}')
if [[ ! "$fingerprint" =~ ^[0-9A-F]{40}$ ]]; then
  echo "could not read a 40-hex fingerprint for $identity" >&2
  exit 1
fi

echo "$fingerprint"
