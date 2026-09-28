# Vendored: Hermes Agent bootstrap (`install-hermes.sh`)

Issue #611: the orchestrator image used to `curl` this bootstrap from the
network at build time and pin its bytes with `HERMES_INSTALL_SHA256`. Upstream
drifted, so the fail-closed `sha256sum -c -` broke the build. The reviewed
script is now vendored in-tree; the build no longer touches the network for the
installer and the hash is asserted against the vendored bytes.

## Provenance

| Field | Value |
|---|---|
| Source URL | `https://hermes-agent.nousresearch.com/install.sh` |
| Fetched (UTC) | 2026-09-28 |
| Bytes | 40413 |
| SHA-256 | `eabdc86a11edc4dd4386215d927677f9a5be40318e20817d101af98b78a1f3b2` |

The vendored file is byte-for-byte identical to the response served by the
source URL at the fetch time above; no header or edits were added, precisely so
its hash equals upstream's and the bytes can be re-verified with:

```sh
curl -fsSL https://hermes-agent.nousresearch.com/install.sh \
  | sha256sum -c <(printf '%s  -\n' eabdc86a11edc4dd4386215d927677f9a5be40318e20817d101af98b78a1f3b2)
```

## Why the pinned hash changed

- Previous Dockerfile pin (from #606): `abca7d8aed691fe608d794bd44e33ffc6f68513c5b4e9270d176701e16afccd9`
- Current vendored bytes: `eabdc86a11edc4dd4386215d927677f9a5be40318e20817d101af98b78a1f3b2`

Upstream drifted again after #606. The Dockerfile's `HERMES_INSTALL_SHA256` is
now pinned to the vendored bytes, so a future change to this file must be a
deliberate, reviewed edit to both the file and `VENDOR.md`.

## Review notes

- The bootstrap is executed as root during `docker build`; treat edits to the
  vendored file as security-sensitive.
- Contract the Dockerfile depends on (present in these bytes):
  `--commit`, `--skip-setup`, `--skip-browser`. The legacy npm-workspace guard
  (`if [ -f "$INSTALL_DIR/package.json" ]; then`) is absent, so the Dockerfile's
  `else` branch (asserting `--skip-browser` is supported) runs.
- `HERMES_COMMIT` is unchanged (`6e69a8933adda7dbbff7cf3009a259a4524477e9`).
