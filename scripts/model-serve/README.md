# Model server automation (standalone mlx Macs)

Exo is retired. Each Mac runs one full model locally:

| Host | Model | mlx port | guard port |
|---|---|---|---|
| `.17` | `mlx-community/Qwen3-Coder-Next-4bit` | 52416 | 52417 |
| `.18` | `mlx-community/Qwen3-Next-80B-A3B-Thinking-4bit` | 52416 | 52417 |

The stack per Mac: `mlx_lm.server` (port 52416) behind `model_guard.py`
(port 52417, a serializing proxy: single-queue, 64 MB body cap, 503+retry when
busy). The orchestrator and the rehearsal harness talk only to the guard.

## Model weights (durable HF cache)

Weights live in the standard Hugging Face cache under the service user's home:

```text
/Users/glasslab/.cache/huggingface/hub/models--<org>--<name>/snapshots/<revision>
```

This path is durable across reboots. The earlier `/private/tmp/hf-cache` cache
was ephemeral on macOS: it was cleared, leaving a dangling/0-byte snapshot, and
`mlx_lm.server` then hung forever on the first completion instead of failing.
`HF_HOME` is the single source of truth in the installer and defaults to
`/Users/glasslab/.cache/huggingface`; override it with
`GLASSLAB_MODEL_HF_HOME` only when that path is equally durable.

### (Re)download a model

Run as the service user. The `hf` CLI ships with the mlx venv:

```bash
HF_HOME=/Users/glasslab/.cache/huggingface \
  /Users/glasslab/exo/.venv/bin/hf download \
  mlx-community/Qwen3-Coder-Next-4bit \
  --revision 7b9321eabb85ce79625cac3f61ea691e4ea984b5
```

For `.18`, use the `thinking` repo/revision:

```bash
HF_HOME=/Users/glasslab/.cache/huggingface \
  /Users/glasslab/exo/.venv/bin/hf download \
  mlx-community/Qwen3-Next-80B-A3B-Thinking-4bit \
  --revision 9a2b46347bb170cb2924092175fa21554fe585a9
```

Re-running the command resumes an interrupted download. When the preflight
fails, the installer prints this exact command with the right repo/revision.

### Completeness preflight

Before writing or bootstrapping any daemon, the installer verifies the target
snapshot is complete:

- `config.json` exists and is not a dangling symlink,
- `model.safetensors.index.json` exists,
- every `model-*.safetensors` shard resolves to a non-empty blob.

An incomplete snapshot aborts the install with the snapshot path, the specific
problem, and the `hf download` repair command. A daemon is never started
against missing or partial weights.

## Install

Run **on the Mac** as a sudo-capable user:

```bash
sudo ./install-model-serve.sh coder        # on .17
sudo ./install-model-serve.sh thinking     # on .18
```

This installs two launchd daemons per Mac:

- `com.glasslab.mlx-{coder,thinking}` — the mlx server, `KeepAlive`,
  `--prompt-cache-size 8`, `HF_HOME=/Users/glasslab/.cache/huggingface`,
  `HF_HUB_OFFLINE=0` (online by default, so a missing/incomplete snapshot can
  be re-downloaded)
- `com.glasslab.mlx-{coder,thinking}.guard` — the serializing guard,
  forwarding to `127.0.0.1:52416/v1`

The guard script is copied from `/tmp/model_guard.py` to
`/usr/local/libexec/glasslab-model-guard.py` (stable across reboots).

### Offline mode (opt-in)

Offline is opt-in; the default allows Hugging Face to fetch missing weights:

```bash
sudo ./install-model-serve.sh coder --offline
# or: sudo GLASSLAB_MODEL_HF_OFFLINE=1 ./install-model-serve.sh coder
```

`--offline` pins `HF_HUB_OFFLINE=1` in both daemons and refuses to re-download.
Use it only once the snapshot is verified complete — offline mode cannot repair
one.

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
sudo -u glasslab -H bash -c 'export HF_HOME=/Users/glasslab/.cache/huggingface; \
  </dev/null /Users/glasslab/exo/.venv/bin/python \
  /Users/glasslab/exo/.venv/bin/mlx_lm.server --model <SNAPSHOT_PATH> \
  --port 52416 --host 0.0.0.0 --decode-concurrency 1 --prompt-concurrency 1 \
  --prompt-cache-size 8 > /Users/glasslab/mlx-<role>.log 2>&1 &'
```

Remember: without `-H` the HOME stays the invoker's, and the weights must
already be complete under `HF_HOME` (the installer's preflight checks this).
Add `HF_HUB_OFFLINE=1` to the export only to force offline.
