# `/ui` Access Model

Status: **decided 2026-10-21** (roadmap #616, stage-2 prerequisite for #595).

**Decision: Option A** — keep the SSH port-forward front door and the single
shared operator token. Do not add an ingress or per-user identity yet. Revisit
Option B when the audience grows beyond the operator allowlist (triggers below).

## Current surface

`/ui` is internal-only:

```text
operator workstation
  -> ssh -L 18080:127.0.0.1:18080 glasslab-provisioner
  -> loopback proxy 127.0.0.1:19090 (page) / 19091 (viewer, --allow-paths /ui/pdf/)
  -> proxy injects X-Glasslab-Operator-Token
  -> research-orchestrator /ui
```

- Scope: fewer than five whitelisted operators.
- Orchestrator is a singleton (`Recreate`, 1 replica); the singleton is fine
  for this audience.
- The page CSP starts from `default-src 'none'`; it is not a public surface.

## Option A — SSH front door + shared operator token (chosen)

**How it works.** The operator already holds SSH access to the provisioner. The
loopback proxy injects the operator token so the token never reaches the
browser. `GET /ui/` and the `/ui/pdf/**` viewer are served on separate loopback
listeners; the viewer allowlist is path-scoped.

**Why.**
- No new authentication, session, CSRF, or ingress surface to design and
  review.
- Matches how the surface is actually used today (internal, few operators).
- The operator token is already the orchestrator's read/write credential, so a
  second identity system would not add authority the operators lack.

**Costs.**
- The token is shared: there is no per-user attribution in `/ui` requests.
- Every operator needs the tunnel + proxy running locally.
- Not appropriate for an audience that cannot hold SSH access.

**CSP consequence for streaming chat (#595).** Stage 2 may widen the page CSP by
exactly one directive:

```text
script-src 'nonce-<per-response-nonce>'
```

Everything else stays: `default-src 'none'`, `object-src 'none'`, `base-uri
'none'`, `form-action 'self'`, `frame-src 'self'` (viewer origin),
`frame-ancestors 'none'`, and the nonce'd `style-src`. No `unsafe-inline`, no
`unsafe-eval`, no external origin. The inline script inserts model text only via
`textContent`, never `innerHTML`.

## Option B — ingress + per-user identity (deferred)

**How it would work.** Gateway ingress plus Discord OAuth (or the existing
Discord role model), server-side sessions, per-user run/artifact scoping, and an
ingress NetworkPolicy. The current orchestrator ingress allows only the kubelet
probe path, so B requires a deliberate policy change.

**Why not now.**
- It is a security-model change, not a UI change: sessions, CSRF, revocation,
  and authorization mapping all become part of the surface.
- It may force revisiting the singleton/scale model (concurrent users, session
  store, sticky routing).
- The current audience does not need it.

**Prerequisites before B.** A per-user identity source; a session store; an
authorization mapping from identity to runs/artifacts; an ingress policy that
admits only the gateway; and a review of the singleton assumption.

## Revisit triggers

Move to B when any of these becomes true:

- more than five concurrent operators, or operators who cannot hold SSH access;
- a requirement for per-user attribution or audit of `/ui` actions;
- access from outside the current SSH-capable group;
- a second tenant or a shared-namespace deployment.

## Invariants that hold under either option

- escape-first rendering; no `innerHTML` for model or corpus text;
- no external fonts, scripts, or CDN; all assets vendored and served
  same-origin;
- the operator token is never printed, logged, or committed;
- the PDF viewer stays on a path-scoped origin (`--allow-paths /ui/pdf/`);
- state-changing POSTs keep the Fetch-Metadata / loopback-origin CSRF guard.

## References

- Roadmap: #616
- Streaming chat: #595
- UI foundation: #592, #618, #619
- Viewer-origin isolation: #620
- Runbooks: `docs/glasslab-v2/runbooks/orchestrator-corpus-ui.md`,
  `orchestrator-corpus-ui-chat.md`
