# Model server automation (standalone mlx Macs)

Exo is retired. Each Mac runs one full model locally:

| Host | Model | mlx port | guard port |
|---|---|---|---|
| `.17` | `mlx-community/Qwen3-Coder-Next-4bit` | 52416 | 52417 |
| `.18` | `mlx-community/Qwen3-Next-80B-A3B-Thinking-4bit` | 52416 | 52417 |

The stack per Mac: `mlx_lm.server` (port 52416) behind `model_guard.py`
(port 52417, a serializing proxy: single-queue, 64 MB body cap, 503+retry when
busy). The orchestrator and the rehearsal harness talk only to the guard.

## Install

Run **on the Mac** as a sudo-capable user:

```bash
sudo ./install-model-serve.sh coder        # on .17
sudo ./install-model-serve.sh thinking     # on .18
```

This installs two launchd daemons per Mac:

- `com.glasslab.mlx-{coder,thinking}` — the mlx server, `KeepAlive`, cache
  `--prompt-cache-size 8`, `HF_HOME=/private/tmp/hf-cache`,
  `HF_HUB_OFFLINE=1` (never re-downloads weights)
- `com.glasslab.mlx-{coder,thinking}.guard` — the serializing guard,
  forwarding to `127.0.0.1:52416/v1`

The guard script is copied from `/tmp/model_guard.py` to
`/usr/local/libexec/glasslab-model-guard.py` (stable across reboots).

## Migrating the manual setup

```bash
sudo ./install-model-serve.sh coder --replace
```

`--replace` stops any non-launchd process bound to 52416/52417 first.

> **Do not pass `--replace` while a rehearsal/run is actively using the model.**
> Killing the server mid-turn kills the in-flight generation.

## Verify

```bash
curl http://127.0.0.1:52417/v1/models          # per Mac
scripts/check-exo-model-split.sh               # from a workstation
```

## After a reboot

LaunchDaemons auto-start both services (that is the point). Model reload takes
~30-60 s; the first request through the guard warms it.

## Manual fallback (if launchd is undesirable)

```bash
sudo -u glasslab -H bash -c 'export HF_HOME=/private/tmp/hf-cache HF_HUB_OFFLINE=1; \
  </dev/null /Users/glasslab/exo/.venv/bin/python \
  /Users/glasslab/exo/.venv/bin/mlx_lm.server --model <SNAPSHOT_PATH> \
  --port 52416 --host 0.0.0.0 --decode-concurrency 1 --prompt-concurrency 1 \
  --prompt-cache-size 8 > /Users/glasslab/mlx-<role>.log 2>&1 &'
```

Remember: without `-H` the HOME stays the invoker's, and without
`HF_HUB_OFFLINE=1` the server may try to re-download the weights from the hub.