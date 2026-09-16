# Validate WhatsApp Command Channel

> **Status: retired.** The WhatsApp gateway, research-ingress, and
> research-command-router were retired under issue #159. This runbook is kept
> for historical reference only; the current operator surface is the
> research-orchestrator Discord workflow in
> [`docs/research-orchestrator-command-surface.md`](../../research-orchestrator-command-surface.md).

1. Log into the provisioner host and move to the canonical repo.

```bash
ssh glasslab@192.168.1.44
cd /home/glasslab/cluster-config
```

2. Confirm the deterministic command surface is healthy.

```bash
./scripts/smoke-test-v2.sh
kubectl -n glasslab-v2 get deploy,svc | egrep 'workflow|research|whatsapp'
```

3. Confirm the WhatsApp gateway secret exists.

```bash
kubectl -n glasslab-v2 get secret glasslab-whatsapp-gateway
```

Expected keys:

- `WHATSAPP_OWNER`
- `WHATSAPP_ALLOW_FROM`

4. Port-forward the live services for direct checks.

```bash
kubectl -n glasslab-v2 port-forward svc/glasslab-research-command-router 18096:8095 &
kubectl -n glasslab-v2 port-forward svc/glasslab-research-ingress 18097:8096 &
kubectl -n glasslab-v2 port-forward svc/glasslab-whatsapp-gateway 18098:8097 &
```

5. Verify router help and unsupported-turn behavior.

```bash
curl -fsS -X POST http://127.0.0.1:18096/dispatch \
  -H 'content-type: application/json' \
  -d '{"message":"!help"}'

curl -fsS -X POST http://127.0.0.1:18096/dispatch \
  -H 'content-type: application/json' \
  -d '{"message":"what do you think?"}'
```

Expected:

- `!help` returns only the supported command surface
- unsupported turns return a deterministic `Use !help` response

6. Verify ingress behavior.

```bash
curl -fsS -X POST http://127.0.0.1:18097/inbound \
  -H 'content-type: application/json' \
  -d '{"message":"!state","sender":"+15555550123","channel":"whatsapp"}'

curl -fsS -X POST http://127.0.0.1:18097/inbound \
  -H 'content-type: application/json' \
  -d '{"message":"what do you think?","sender":"+15555550123","channel":"whatsapp"}'
```

Expected:

- supported commands return `route=deterministic-router`
- unsupported turns return `route=unsupported-turn`

7. Verify gateway health.

```bash
curl -fsS http://127.0.0.1:18098/healthz
```

Expected:

- no chat-backend or OpenClaw fallback fields
- only deterministic gateway config and policy fields

8. Send a real WhatsApp command through the provider path and verify backend proof.

Recommended deterministic checks:

- `!state`
- `!new replicate DreamSim visual similarity metric with PyTorch and timm`
- `!plan`
- `!check`

Backend proof:

```bash
kubectl -n glasslab-v2 logs deploy/glasslab-workflow-api --since=5m | tail -n 200
kubectl -n glasslab-v2 logs deploy/glasslab-whatsapp-gateway --since=5m | tail -n 200
```

Success condition:

- the turn is handled by the repo-owned path
- no OpenClaw object is involved
- the response text comes back directly from the deterministic backend chain
