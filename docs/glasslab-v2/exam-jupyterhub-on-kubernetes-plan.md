# Exam JupyterHub On Kubernetes

Status: **a first version is live** for the DSEI 10200 exam (2026-10-21). It was
built on 2026-10-20 with the faster of the two options below (Zero to JupyterHub)
and served `https://glasslab.org/exam/` from 17:46 that day. The section
"As built" records what is running and how it differs from this plan. The rest
of the document is the target design for future exams and for researcher
JupyterHubs. It was written the same day, before the build.

## As built (2026-10-20)

Live configuration: [`../../kubeadm/glasslab-exam/`](../../kubeadm/glasslab-exam/README.md).

| Area | Live | Plan below | Why it differs |
|---|---|---|---|
| Packaging | Z2JH chart **4.4.0** (JupyterHub 5.5.0, KubeSpawner 7.0.0, NativeAuthenticator 1.3.0), release `exam`, Helm v3.22.0 from the provisioner | Option A: Mike's hub image + KubeSpawner, plain manifests | The exam was next day; Z2JH needs no image build, and its hub bundles the same JupyterHub and NativeAuthenticator versions as Mike's. Roster logic ported into `hub.extraConfig` |
| Student image | Mike's `exam-student:release`, unchanged, imported into containerd on node01/03/04/05 (manifest `ddf97c44…`) | GHCR by digest | No time to publish; import is the documented break-glass path |
| Student storage | **Local** hostPath folders: odd students + `examadmin` on node01, even on node05, `/var/lib/glasslab-exam/users/<name>` (1000:1000, 0700); a `pre_spawn_hook` pins each student to their node | NFS on `.207` | The `.207` export squashes every non-root UID: as root a pod can create and `chown` folders, but UID 1000 cannot write even into a `0777` folder, over NFSv4.1 or NFSv3. Fixing it needs the NAS export changed to "no mapping" for non-root users (§9) |
| Hub state | Static hostPath PV `glasslab-exam-hub-db` on node03, pre-bound to the chart's `hub-db-dir` PVC | Local PV on the hub node | As planned |
| Isolation | Chart NetworkPolicies with every `egressAllowRules` off: students reach only the hub API (8081) and the proxy's public port (8000, which the chart always allows). `hub_connect_url` is the hub Service IP, resolved at hub start, so students need no DNS | Namespace default-deny, fixed ClusterIP, VAP image pinning, PSA `restricted` | PSA `restricted` would reject the hostPath volumes; no VAP was written. The proxy port is the same login-protected site the browser reaches |
| Exposure | NodePort `proxy-public` 30800, `externalTrafficPolicy: Cluster`; gateway `exam-route.conf` → `192.168.1.50:30800` | `Local` + gateway-only ingress rule | Every node's 30800 is reachable from the lab LAN (not from campus) |
| PIDs | No per-pod limit | kubelet `podPidsLimit: 256` on exam nodes | Kubelet change skipped for time |
| Fallback | node02 Docker hub still running; `exam-bundle/gateway/switch-exam-route.sh node02` on the gateway switches back | Docker host prepared from T-7 d | Same idea |

Tests run on the live cluster before the gateway switch, all passed:

- **Session** (`exam-bundle/tests/student-session.py`): login, spawn, JupyterLab, a kernel over
  WebSocket running numpy/pandas/scikit-learn/matplotlib, notebook save/delete. Run as student23
  via the NodePort, and as student22 through `https://glasslab.org/exam/` after the switch.
- **Pod security**: UID/GID 1000, all capabilities dropped, no privilege escalation, seccomp
  `RuntimeDefault`, no service-account token, 1 CPU / 1 GiB limits (0.25 CPU / 512 Mi requests).
- **Isolation** (`kubeadm/glasslab-exam/netpol-probe.sh`): from a student-labelled pod, only the hub
  API and proxy public port answer. Other students, the proxy admin API, the Kubernetes API, cluster
  and external DNS, `glasslab-v2` Postgres, node SSH and kubelet, the provisioner, the gateway,
  node02, the internet and `169.254.169.254` are all blocked. An unlabelled control pod reached the
  internet, the API, DNS and the nodes, which shows the probes detect openings.
- **Load** (Mike's workload, `exam-bundle/labbox/load-test-k8s.sh`): all 20 ready in 43.7 s
  (≤ 120), synchronized pass slowest 15.6 s (≤ 60), staggered 12.6 s. node01 and node05 kept ≥ 85 %
  memory available with no swap and no OOM kills. node02 alone measured 66.9 s / 28.7 s.

Not done in the live version: Mike's restore and backup drills, hub-restart and node-loss drills,
the per-pod PID limit, and a real CCNY lab browser check (pending for the morning of the exam).

Build notes worth keeping:

- On 2026-10-20 between about 17:05 and 17:30, GitHub, PyPI, quay.io, GHCR and the Docker Hub
  registry were unreachable from the whole campus (the gateway too) while Google answered, and Red Hat
  reported quay.io degraded. Pre-pull or import every image an exam needs well ahead of time.
- The lab router's hairpin NAT loses about 1 in 15 new connections from the lab LAN to the gateway's
  public IP. Lab-LAN clients should resolve `glasslab.org` to `192.168.1.4`.

## Why this exists

For the DSEI 10200 exam on 2026-10-21 (5:00–7:30 PM), Mike's JupyterHub release
(`exam-hub:release`, `exam-student:release`, DockerSpawner, single-host design)
was deployed unchanged with Docker Compose on `node02` (`192.168.1.11`). `node02`
was cordoned, drained and taken out of Kubernetes (kubelet disabled) for the
exam week. Students reach it at `https://glasslab.org/exam/` through the gateway's
Apache. It passed a 20-student load test on 2026-10-20: all 20 servers ready in
66.9 s and a synchronized pass of 28.7 s at worst, against targets of 120 s and 60 s.

The single host was chosen because the exam date left no time to port and retest.
That worked, but it costs a worker node for a week, relies on a hand-built host
firewall, and leaves one disk holding every student's work. This plan describes
how to run the same release on the Glasslab Kubernetes cluster for future exams,
and what has to pass before it replaces the single host.

Sources this plan builds on:

- Mike's release source: `hub/jupyterhub_config.py`, `hub/Dockerfile`,
  `hub/requirements.in` / `.lock`, `scripts/resolve.sh`, `scripts/backup.sh`,
  `tests/load-local.py`, `release/manifest.json`. On node02 it lives under
  `/opt/exam-release`.
- The exam deploy bundle used for node02 (`~/exam` on node02; not in this
  repository): `labbox/exam-firewall-check.sh`, `labbox/load-test.sh`,
  `tests/make-load-exam.py`, `tests/student-session.py`,
  `gateway/exam-route.conf`, `node02-runbook.md`.
- The gateway `/exam/` route, recorded in
  [ccny-glasslab/glasslab-cluster-config#699](https://github.com/ccny-glasslab/glasslab-cluster-config/pull/699)
  (stacked on #696 and #698), and [`../gateway-web.md`](../gateway-web.md).
- Repo conventions: [`internal-service-exposure.md`](internal-service-exposure.md),
  [`image-distribution.md`](image-distribution.md),
  [`../../kubeadm/glasslab-v2/storage/README.md`](../../kubeadm/glasslab-v2/storage/README.md),
  [`../../kubeadm/calico-installation.yaml`](../../kubeadm/calico-installation.yaml),
  [`runbooks/rollout-freeze-preflight.md`](runbooks/rollout-freeze-preflight.md).

## Goals and non-goals

Goals:

- Same student experience and the same security properties as the node02 deployment:
  same images, same authenticator and roster rules, same per-student limits, same
  network isolation, same URL.
- No borrowed node. Student servers spread over designated workers, and the
  cluster keeps running research work outside the exam window.
- Every property is checked by an executable test, not just stated in prose.

Non-goals for the first cluster version:

- Hub high availability. The hub stays a single pod with SQLite on a local
  volume, as today. Section 8 covers the recovery story.
- Changing the student-facing workflow, Mike's roster/NativeAuthenticator model,
  or the student image.
- Exposing anything beyond `/exam/` through the gateway, or adding a cluster
  ingress controller.

## Summary of decisions

| Area | Recommendation | Main alternative | Live (2026-10-20) |
|---|---|---|---|
| Spawner / packaging | Mike's hub image rebuilt with `jupyterhub-kubespawner` added to the hash-locked requirements (DockerSpawner removed), deployed with plain manifests under `kubeadm/glasslab-exam/` | Zero to JupyterHub chart 4.4.0 (JupyterHub 5.5.0) | Z2JH 4.4.0 |
| Namespace | New `glasslab-exam`, Pod Security Admission `restricted` | — | `glasslab-exam`, no PSA label (hostPath storage) |
| Student storage | Static NFS PV on the `.207` NAS (dedicated export path), one RWX PVC, per-student `subPath` | Local PVs; a dynamic NFS provisioner | Local hostPath on node01/node05, students pinned (NAS squashes non-root) |
| Hub state (SQLite) | Static local PV on the hub's node. Never on NFS | Postgres `db_url` | As planned (node03) |
| Isolation | Namespace default-deny NetworkPolicies (Calico enforces), DNS-free student pods, an admission policy pinning images | Calico `GlobalNetworkPolicy` | Chart policies, all egress allow-rules off, DNS-free; no VAP |
| Exposure | NodePort Service with `externalTrafficPolicy: Local`, ingress allowed only from the gateway IP, gateway Apache retargeted. A recorded exception to the internal-only rule | `hostPort`; MetalLB; internal ingress | NodePort 30800, `Cluster` policy (LAN-reachable) |
| Images | Private GHCR packages by digest, pre-pulled onto exam nodes before the exam | containerd import from Mike's tarball (break-glass) | containerd import |
| Node placement | 2–3 workers labelled and tainted for the exam window, with `podPidsLimit: 256` in their kubelet config | Shared nodes, with the PID limit relaxed | node01 + node05 for students, node03 for hub/proxy; no taints, no PID limit |

## 1. Spawner

### 1.1 Option A (recommended): Mike's hub image plus KubeSpawner

Keep Mike's build exactly as it is and change only the spawner:

1. `hub/requirements.in`: drop `dockerspawner==14.0.0` (which pulls in `docker==7.2.0`)
   and add `jupyterhub-kubespawner==7.1.0`. That is the current release, and it
   requires `jupyterhub>=4.0.0`, `kubernetes-asyncio`, `escapism`,
   `python-slugify`, `jinja2`, `pyyaml` and `urllib3`.
2. Regenerate `hub/requirements.lock` with Mike's own `scripts/resolve.sh`
   (`uv pip compile --generate-hashes`, Python 3.12, `x86_64-unknown-linux-gnu`).
   The Dockerfile already installs with `--require-hashes`, so the new
   dependencies are hash-locked the same way. Review the lock diff. It should
   add only kubespawner's dependency tree and keep `jupyterhub==5.5.0`,
   `jupyterhub-nativeauthenticator==1.3.0`, `sqlalchemy==2.1.4` and
   `tornado==6.5.10` unchanged.
3. Base image unchanged: `quay.io/jupyterhub/jupyterhub:5.5.0@sha256:f91ed45a…`,
   which also provides `configurable-http-proxy`. The hub and its proxy stay in one
   pod, as on node02, so the network matrix keeps the same shape.
4. Add a Kubernetes config file (`jupyterhub_config_k8s.py`). Better still, make
   the spawner section of the existing config switchable by an environment
   variable so one image serves both deployments. The roster check,
   `RosterAuthenticator`, NativeAuthenticator settings, timeouts, spawn limits and
   environment stay byte-for-byte the same. The Docker-only lines go:
   `from docker.types import LogConfig`, `DockerSpawner.*` and the
   `EXAM_STORAGE_VOLUME` branch.
5. Tag the result as a new release (for example `exam-hub:k8s-<date>`) with its
   own `release/manifest.json` entry. Don't reuse the `exam-hub:release` tag.

Who builds it is a decision for Mike and the Glasslab maintainers (section 9).
The cleanest split keeps the hub/student image source in Mike's release, with
cluster-config holding only manifests, configuration, tests and runbooks.

### 1.2 Option B: Zero to JupyterHub

The Z2JH chart index maps chart `4.4.0` to JupyterHub `5.5.0` (released
2026-06-10). The latest, `4.4.3`, ships `5.5.3`. A Z2JH deployment would pin
`4.4.0` for version parity.

What it would add: a separate proxy pod and `proxy-public` Service (NodePort
supported), a continuous image pre-puller, chart-managed NetworkPolicies, an
optional user scheduler, and a well-trodden upgrade path.

Why it is not the first choice here:

- The hub runs the chart's `k8s-hub` image, not Mike's. His hash-locked
  provenance is replaced by the chart's frozen requirements. The
  NativeAuthenticator version bundled there has to be checked against `1.3.0`.
  The roster logic moves into `hub.extraConfig`.
- The chart's default single-user egress allows non-private IPs (the internet).
  Every policy default has to be overridden to reach the matrix in section 3, and
  those overrides would need their own review.
- The repository has no Helm usage today. Adding it brings a new tool, a new
  rendering step and a new rollback model for one service.
- Splitting proxy from hub changes the topology that node02 tested.

Revisit Z2JH if Glasslab ends up hosting several JupyterHubs, where the
chart's maintenance pays off.

### 1.3 Mapping Mike's limits to the pod spec

| Mike's DockerSpawner setting | KubeSpawner / Kubernetes equivalent | Notes |
|---|---|---|
| `image = STUDENT_IMAGE`, `allowed_images = []`, `pull_policy = 'never'` | `c.KubeSpawner.image = '<ghcr>/exam-student@sha256:…'` (digest), no `profile_list`, no options form, `image_pull_policy = 'IfNotPresent'` | Users cannot pick an image. A ValidatingAdmissionPolicy (§3.4) also rejects any pod in the namespace whose image is not on the digest list, so a compromised hub cannot launch other images |
| `cpu_limit = 1.0` | `cpu_limit = 1`, `cpu_guarantee = 0.4` | Docker set no reservation. Kubernetes needs a request for scheduling. 0.4 × 20 = 8 cores reproduces node02's measured density. Tune from the load test |
| `mem_limit = 1G` | `mem_limit = '1G'`, `mem_guarantee = '1G'` | JupyterHub's `G` is GiB, the same as Docker's. Request = limit avoids memory overcommit during the exam |
| `pids_limit = 256` | kubelet `podPidsLimit: 256` on the exam nodes | Kubernetes has no per-pod PID field. The kubelet setting covers every pod on the node, which is one reason for dedicated exam nodes (§6). Checked with a fork test, not by reading `pids.max` |
| `extra_create_kwargs user 1000:1000` | `uid = 1000`, `gid = 1000`, `fs_gid = None`, `supplemental_gids = []`, `container_security_context.runAsNonRoot = true` | Leaving `fsGroup` unset avoids recursive chowns. Ownership on the NFS export is prepared once (§2) |
| `cap_drop ALL` | `container_security_context.capabilities.drop = ['ALL']` | |
| `no-new-privileges:true` | `allow_privilege_escalation = False` (the KubeSpawner default, set explicitly) | |
| Docker's default seccomp profile (implicit) | `container_security_context.seccompProfile.type = RuntimeDefault` | Kubernetes runs pods `Unconfined` unless asked. Required for parity and by PSA `restricted` |
| No Docker socket or credentials in student containers | `automount_service_account_token = False`, no `service_account` | The default with no service account. Set explicitly and probed |
| — (Docker internal network had no service env) | `extra_pod_config = {'enableServiceLinks': False}` | Stops Kubernetes injecting `*_SERVICE_HOST` variables for every Service in the namespace |
| `disable_user_config = True` | unchanged (`c.Spawner.disable_user_config`) | |
| `name_template exam-user-{username}`, `remove = True` | `pod_name_template = 'exam-user-{username}'`; stopped pods are deleted by default | The roster regex limits names to `student[0-9]{2}`/`examadmin`, so KubeSpawner 7's escaping is a no-op |
| `volumes users/{username} → /home/jovyan/work` | `volumes`: the RWX PVC; `volume_mounts`: `mountPath /home/jovyan/work`, `subPath users/{username}` | `/home/jovyan` stays in the container's writable layer, as on node02. Only `work/` persists |
| `pre_spawn_hook require_storage` | kept. The hub mounts the same PVC read-only at `/exam-users` | The hook's `lstat` needs only search permission on `users/` (§2.3). The `storage_volume` branch is deleted |
| `network_name`, `use_internal_ip` | `hub_connect_url = 'http://<fixed hub ClusterIP>:8081'` | Students reach the hub without DNS (§3.2) |
| `log_config json-file 10m × 3` | kubelet container log rotation (default 10 Mi × 5) | |
| `start_timeout 120`, `http_timeout 60`, `concurrent_spawn_limit 5`, `active_server_limit 26` | unchanged | |
| — | `node_selector {'glasslab.org/exam': 'true'}`, matching toleration, a soft `topologySpreadConstraints` across hostnames via `extra_pod_config` | Spreads students over the exam nodes |

The hub pod gets its own restricted context: a non-root UID (the base image runs
as root by default, which PSA `restricted` refuses), `runAsNonRoot`, drop ALL,
`RuntimeDefault` seccomp, and `/srv/jupyterhub` on the local PV owned by that UID.
Mike's `os.umask(0o077)` stays. Running the hub non-root has to be confirmed in the
rehearsal. If it fails, move the hub into its own namespace at PSA `baseline`. Do
not weaken the student namespace.

## 2. Storage

The cluster has no default StorageClass, and the repo's storage policy is explicit,
reviewable static PV/PVC wiring
([`storage/README.md`](../../kubeadm/glasslab-v2/storage/README.md)).

### 2.1 Options

| Option | Fit for student work | Fit for hub SQLite |
|---|---|---|
| **Static NFS PV on `.207`, one RWX PVC, per-student `subPath`** | **Recommended.** Pods can land on any exam node, and work survives node loss. Same pattern as [`20-nfs-static-pv.yaml`](../../kubeadm/glasslab-v2/storage/20-nfs-static-pv.yaml) (`nfsvers=4.1`, `Retain`, `storageClassName: ""`) | **No.** SQLite locking over NFS is unreliable, and the hub DB holds the NativeAuthenticator password hashes |
| **Static local PV** (node-pinned, like Postgres/MinIO/NATS) | Poor. One PV per student per node, or everything pinned to one node, which recreates node02 | **Recommended.** Small, RWO, pins the hub to one known node. That node is also the gateway's NodePort target |
| Dynamic provisioner (`csi-driver-nfs`, `nfs-subdir-external-provisioner`, `local-path`) | Works, but it is a new cluster-wide component and a default-StorageClass decision made for 26 directories | Same objection |
| Postgres (`db_url` to `glasslab-postgres`) | — | Rejected for now: it couples exam availability to the research database, adds a driver to the hash lock, and needs a cross-namespace hole |

### 2.2 NFS layout

- Prefer a **dedicated** Synology share or export for exam data, restricted to the
  worker IPs, over another subdirectory of `/volume1/backup`. The existing
  `glasslab-v2` PVs point into that export, and any node that can mount the export
  root can read everything in it. Fallback if a new share is refused:
  `/volume1/backup/glasslab-exam/<exam-id>/`.
- Per-exam directory: `<root>/<exam-id>/users/studentNN`, so each exam's work
  stays separate and can be exported and retained on its own.
- PV `glasslab-exam-<exam-id>-users-pv` (RWX, `Retain`, `claimRef` to
  `glasslab-exam/exam-users`). The PVC is mounted with `subPath users/{username}`
  in student pods and read-only at `/exam-users` (subPath `users`) in the hub.
- Student pods cannot reach the NAS over the network (§3). The kubelet mounts NFS
  from the node, so the NetworkPolicies don't affect the mount itself.

### 2.3 Ownership and permissions

Mike's hook requires each `users/studentNN` to be a real directory owned by
`1000:1000` with mode `0700`.

- The Synology export must pass UID 1000 through unchanged ("No mapping"). Whether
  root is squashed has to be checked (§9). The plan doesn't need root on the NAS.
- `users/` is owned by `1000:1000`, mode `0711`. Students can't list it, and the
  non-root hub can still `lstat` children.
- A one-shot prep Job (in the repo, UID 1000, no capabilities) creates
  `users/studentNN` at `0700` from the roster before accounts open. Kubernetes
  would otherwise create a missing `subPath` itself, with the wrong owner.
- Capacity: Mike's load test samples disk headroom (target ≥ 20 %). On the
  cluster it reads the PV's filesystem stats from the kubelet summary API (§7.3).

### 2.4 Hub state

Local PV `glasslab-exam-hub-state-pv`, `/var/lib/glasslab-exam/hub-state` on the
chosen hub node, owned by the hub UID, mode `0700`. It holds `jupyterhub.sqlite`
and `jupyterhub_cookie_secret`. Use a fresh directory per exam, created by the
node-prep step. Back it up at the checkpoints in §5.3.

## 3. Isolation

Calico is the CNI (operator `Installation`, IPv4 pool `10.244.0.0/16`, VXLAN). It
enforces standard `networking.k8s.io/v1` NetworkPolicy, which the repo already uses
(for example
[`45-egress-network-policy.yaml`](../../kubeadm/glasslab-v2/research-orchestrator/45-egress-network-policy.yaml)).
The cluster is IPv4-only, so the node02 rule "all IPv6 on `br-exam` dropped" has
no counterpart to build. The probe still checks for it.

### 3.1 Policies (namespace `glasslab-exam`)

| Policy | Selects | Allows |
|---|---|---|
| `default-deny` | all pods | nothing in or out (`policyTypes: [Ingress, Egress]`, empty rules) |
| `hub-ingress` | `app.kubernetes.io/component: exam-hub` | TCP 8000 from `ipBlock 192.168.1.4/32` (the gateway's LAN IP); TCP 8081 from pods labelled `component: singleuser-server` |
| `hub-egress` | hub | TCP 8888 to `component: singleuser-server` pods; TCP 6443 to `ipBlock 192.168.1.49/32` (the API server endpoint after kube-proxy DNAT of `10.96.0.1:443`); nothing else, not even DNS. The in-cluster client uses `KUBERNETES_SERVICE_HOST`, an IP |
| `student-egress` | `component: singleuser-server` (KubeSpawner's default label) | TCP 8081 to the hub pod only |
| `student-ingress` | student pods | TCP 8888 from the hub pod only |
| `backup-egress` (only while a backup Job runs) | `app.kubernetes.io/component: exam-backup` | the backup target only (§5.3) |

What this covers, matching node02: no peer traffic, no hub `:8000` from students,
no host, LAN, NAS, gateway, provisioner, internet or link-local metadata, and no
DNS. It also covers the cluster-only paths node02 never had: the Kubernetes API,
CoreDNS, the `glasslab-v2` services (Postgres, MinIO, NATS, RabbitMQ,
workflow-api), other namespaces' pods, NodePorts, and the kubelet on `:10250`.

Calico applies a pod's egress policy before its own `DefaultEndpointToHostAction`,
so pod-to-own-node traffic is denied by `default-deny`. Calico always allows
host-to-local-pod traffic, so kubelet probes still reach the hub. Both behaviours
are probed rather than assumed.

### 3.2 No DNS for student pods

On node02, students resolved `exam-hub` through Docker's embedded DNS, and external
names failed because the network was internal. In Kubernetes, allowing CoreDNS
would also resolve external names, which is a DNS exfiltration channel. Instead,
the hub Service gets a **fixed `clusterIP`** picked from `10.96.0.0/12` and checked
unused. `c.JupyterHub.hub_connect_url` uses that IP, so `JUPYTERHUB_API_URL` needs
no lookup. Calico evaluates policy after DNAT, so the `student-egress` rule naming
the hub pod also matches traffic sent to that ClusterIP.

### 3.3 Pod Security Admission

Label the namespace `pod-security.kubernetes.io/enforce: restricted` (plus `audit`
and `warn`). That rejects privileged pods, hostPath, host namespaces, added
capabilities, root, and missing seccomp profiles, whoever creates the pod,
including the hub's service account.

### 3.4 Image pinning

A `ValidatingAdmissionPolicy` (GA since Kubernetes 1.30; the cluster runs v1.35)
bound to `glasslab-exam` rejects any Pod whose container or initContainer images
are not on a short digest list: the hub, the student image, and the pinned backup
helper. It also rejects any non-hub Pod with `automountServiceAccountToken` not
`false`. This replaces DockerSpawner's `allowed_images = []` at the API boundary.

### 3.5 Executable probe matrix: `exam-netpol-check.sh`

A new script modelled on `labbox/exam-firewall-check.sh` (same PASS/FAIL output
and exit code). It runs from the provisioner with the canonical kubeconfig.

Static checks:

- the namespace exists with PSA `enforce=restricted`
- the NetworkPolicies match the expected set exactly: names, selectors and rules,
  compared as normalised JSON
- the VAP and its binding are present
- RBAC: `kubectl auth can-i --list` for the hub service account equals the expected
  list (§5.2), and the default service account can do nothing
- the hub Service has the expected fixed ClusterIP, NodePort and
  `externalTrafficPolicy: Local`
- the exam nodes are labelled and tainted, and `podPidsLimit` is 256 in their live
  kubelet config (`/api/v1/nodes/<n>/proxy/configz`)

Live probes use a throwaway listener pod and a probe pod. Both carry the student
labels, use the student image and the full student security context, and land on
each exam node in turn (one node-local and one cross-node pair):

| From → to | Expect |
|---|---|
| hub pod → listener `:8888` (via `kubectl exec`) | open |
| student → hub ClusterIP `:8081` | open |
| student → hub pod IP `:8081` | open |
| student → hub `:8000` | blocked |
| student → another student `:8888`, same node and other node | blocked |
| student → own node IP `:22`, `:10250`, hub NodePort | blocked |
| student → another node IP `:22` | blocked |
| student → API server `10.96.0.1:443` and `192.168.1.49:6443` | blocked |
| student → CoreDNS `10.96.0.10:53`; resolve `example.com`; resolve `kubernetes.default` | blocked |
| student → `glasslab-v2` Postgres `:5432`, workflow-api `:8080`, MinIO `:9000` (ClusterIPs) | blocked |
| student → NAS `192.168.1.207:2049` | blocked |
| student → gateway `192.168.1.4:80`, provisioner `192.168.1.44:22`, exo `192.168.1.17:52417` | blocked |
| student → internet `1.1.1.1:443` | blocked |
| student → `169.254.169.254:80` | blocked |
| student → any IPv6 address | blocked (no address) |
| gateway → hub node NodePort `/exam/hub/health` (via `ssh glasslab-gateway curl`) | open |
| provisioner → hub node NodePort | blocked (node02 parity: "port 8000 answers the gateway only") |
| any node without the hub pod → NodePort | no answer (`Local` policy) |

In-pod assertions in the probe pod: `id` is `1000:1000`; `CapEff` in
`/proc/self/status` is `0`; `NoNewPrivs` is `1`; `Seccomp` is `2`;
`/var/run/secrets/kubernetes.io` is absent; no `*_SERVICE_HOST` env vars;
cgroup `memory.max` is `1073741824` and `cpu.max` is `100000 100000`. A fork test
caps out near 256 processes. The work dir is the student's own subPath, and `..`
does not reach other students' folders.

## 4. Exposure

### 4.1 Today

The gateway's `glasslab-ssl.conf` includes `exam-route.conf`, which proxies
`/exam/` to `http://192.168.1.11:8000/exam/` with `upgrade=websocket timeout=3600`,
`ProxyPreserveHost On`, `X-Forwarded-Proto https`, and
`Require ip 134.74.0.0/16 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16`. TLS ends at
the gateway. The hop from the gateway to the hub is plain HTTP on the lab LAN.

### 4.2 Recommended: NodePort, `externalTrafficPolicy: Local`, gateway-only

- Service `exam-hub-public`, `type: NodePort`, port 8000, fixed `nodePort` (for
  example `30800`), `externalTrafficPolicy: Local`.
- The hub is pinned to one node by its local PV, so the NodePort answers only on
  that node. The gateway route changes one line:
  `ProxyPass /exam/ http://<hub-node-ip>:30800/exam/` (plus `ProxyPassReverse`).
  The campus allow list, WebSocket settings and headers stay the same.
- `Local` preserves the client source IP, so `hub-ingress` can allow only
  `192.168.1.4/32`. With the default `Cluster` policy the source would be SNATed
  to a node IP and the gateway-only rule could not be enforced. A host firewall
  can't do this job either: kube-proxy DNATs NodePort traffic in `PREROUTING`,
  before `INPUT`.
- A separate ClusterIP Service `exam-hub` carries `:8081` with the fixed
  `clusterIP` from §3.2.

### 4.3 Alternatives considered

| Path | Why not first |
|---|---|
| `hostPort` on the hub pod | Same effect, but less visible in Service objects. It depends on the Calico portmap plugin, and the source-IP behaviour needs separate proof. Reasonable fallback |
| MetalLB / `LoadBalancer` | New cluster component and LAN IP allocation for one service |
| Internal ingress controller | New component. The public edge stays the gateway Apache either way, so it adds a hop without removing one |
| `kubectl port-forward` from the gateway | Not an operating path for 20+ WebSocket sessions |

### 4.4 Relation to `internal-service-exposure.md`

That document says every v2 Service is ClusterIP and "Do not add public ingress
for v2 services." The exam hub is a deliberate, recorded exception:

- separate namespace (`glasslab-exam`), not part of the v2 platform
- reachable only from the gateway IP (NetworkPolicy), and through the gateway only
  from campus and private ranges (Apache `Require ip`)
- exposed only during an exam window. Outside it the NodePort Service is deleted
  or the hub is scaled to 0

When this is implemented, add an "Exceptions" section to
[`internal-service-exposure.md`](internal-service-exposure.md) that names this
Service and links here. Optional hardening, not needed for parity: TLS on the
gateway-to-node hop (a cluster-internal certificate, `ProxyPass https://…`), so
passwords don't cross the LAN in clear text.

## 5. Images, RBAC, backup and restore

### 5.1 Image distribution

| | GHCR (recommended) | containerd import |
|---|---|---|
| Fit with repo policy | The supported path ([`image-distribution.md`](image-distribution.md)) | Documented as break-glass only |
| Provenance | Digest-pinned. Each image ID recorded against Mike's `release/manifest.json` | Checksums from Mike's tarball (`exam-images-amd64.tar.sha256`), verified per node |
| Scheduling | Any exam node can pull | Only nodes that were imported to |
| Exam-time dependency | None once pre-pulled (`IfNotPresent`) | None |

Plan:

1. Push the hub (K8s variant) and student images to private
   `ghcr.io/ccny-glasslab/exam-hub` and `exam-student` packages. Record
   tag → digest → local image ID. The image ID (config digest) must equal the
   manifest's `id`. Mike's images are built outside GitHub Actions, so a reviewed
   CI build from his release source is a later step, not a prerequisite.
2. Create the pull secret in the namespace with the existing
   [`create-ghcr-pull-secret.sh`](../../scripts/create-ghcr-pull-secret.sh)
   (`--namespace glasslab-exam`). Attach it to the hub's pod spec and through
   `c.KubeSpawner.image_pull_secrets`.
3. **Pre-pull** on every exam node at T-1 day, with a DaemonSet (or one Job per
   node) that runs both images with a no-op command, then delete it. The student
   image is ~830 MB, and 20 cold pulls at spawn time would blow the 120 s target.
4. Fallback if GHCR is unavailable: containerd import from Mike's tarball
   (`ctr -n k8s.io images import`), checksum-verified, on each exam node.

### 5.2 RBAC for the hub

ServiceAccount `exam-hub` in `glasslab-exam`, with a namespaced Role and
RoleBinding only (no ClusterRole):

| Resource | Verbs | Why |
|---|---|---|
| `pods` | get, list, watch, create, delete | spawn and stop student servers |
| `events` | get, list, watch | spawn progress messages |

No `persistentvolumeclaims` (the PVC is static), no `services` (KubeSpawner's
`services_enabled` stays off), no `secrets` (no internal SSL). Confirm the list in
the rehearsal with `kubectl auth can-i --list --as=system:serviceaccount:glasslab-exam:exam-hub`.
The VAP (§3.4) and PSA (§3.3) together limit what those pod-create rights can
actually create.

### 5.3 Backup and restore

Mike's `scripts/backup.sh` refuses a local repository. It snapshots hub state with
`snapshot-state.py`, adds the release source archive and the user folders, and
runs `restic backup`. Keep that logic and change only where it runs.

- **Target:** a restic repository off the NAS, so a NAS loss doesn't take the
  backup with it. Proposed: an `s3:` repository on Glasslab MinIO (node01 local
  disk), using a scoped user as in
  [`runbooks/provision-minio-scoped-users.md`](runbooks/provision-minio-scoped-users.md),
  plus a `backup-egress` NetworkPolicy here and a matching ingress rule on MinIO.
  The restic password is a Secret created from SOPS, never committed. Off-site is
  better and is a §9 decision.
- **Runner:** a Job using `restic/restic:0.18.1` (digest from Mike's manifest, on
  the VAP list), UID 1000. It mounts the users PVC read-only and the hub-state PV.
  Because the hub-state PV is RWO and node-pinned, the Job is pinned to the hub
  node.
- **Checkpoints:**
  1. *Accounts closed* (signup off, before the exam): hub state only. This is the
     one copy of the password hashes.
  2. *During the exam* (optional): a CronJob every 15 minutes, student work only,
     crash-consistent, as Mike warns.
  3. *Final* (after the exam): stop all servers, scale the hub to 0, run the full
     backup, then scale the hub back up for instructor access if needed.
- **Restore:** into a scratch namespace `glasslab-exam-restore` with new PVs.
  Validate with Mike's `tests/restore-config.py`, `scripts/verify-local-restore.py`
  and `tests/verify-restored-workflow.py`. Restoring at least once is an acceptance
  test (§7).
- **Grading export:** the instructor gets the per-exam `users/` tree from the NAS
  (or from the restic snapshot), not from running pods.

## 6. Node placement and the exam window

- Pick 2–3 workers after a live capacity check. node02 alone (8 cores, 62 GiB)
  carried 20 students. Two nodes give headroom and survive losing one node's
  students.
- **T-2 days:** label `glasslab.org/exam=true` and taint
  `glasslab.org/exam=true:NoSchedule`. Run the
  [rollout-freeze preflight](runbooks/rollout-freeze-preflight.md). Let running
  research jobs finish, then drain non-DaemonSet pods from those nodes, as for
  node02 but without disabling the kubelet.
- Apply kubelet `podPidsLimit: 256` to those nodes through Ansible, then restart
  the kubelet (drained, so nothing is lost).
- After the exam and the final backup: remove the taint, restore the default
  kubelet config, and uncordon.

## 7. Acceptance tests

Run all of them in a rehearsal namespace on the live cluster outside any exam
window, then again against the real exam deployment at T-3 days.

1. **Manifest checks:** `./scripts/check-before-push.sh --configs`, plus
   `kubectl apply --dry-run=server` for every manifest.
2. **Isolation:** `exam-netpol-check.sh` static and `--probe` (§3.5). All PASS.
3. **Student session smoke:** `tests/student-session.py` (login, spawn, JupyterLab,
   kernel over WebSocket running numpy/pandas/sklearn/matplotlib, save, delete)
   - directly against the NodePort from the gateway host (the only allowed source)
   - through `https://glasslab.org/exam/` from a campus-range host, then a real
     browser in a CCNY lab
4. **Load test (Mike's workload, adapted):** a `load-test-k8s.sh` +
   `make-load-exam.py` variant. `make-load-exam.py` already rewrites paths for
   `/exam/`, XSRF and the WebSocket Origin. The k8s variant changes only:
   - preconditions: no `component=singleuser-server` pods running, and the
     `student01-20` folders empty (checked by a UID 1000 Job on the PVC);
   - the websocket client: `websocket-client` 1.9.2 installed from the student
     image's hash lock into a venv under `/home/<user>/venvs/` on the provisioner
     (never `/tmp`), not copied out with `docker cp`;
   - resource sampling: node memory and filesystem from the kubelet summary API
     (`/api/v1/nodes/<n>/proxy/stats/summary`) for each exam node, plus the users
     PV's volume stats. This replaces `/proc/meminfo` and `statvfs(EXAM_ROOT)`;
   - OOM detection: pod `lastState.terminated.reason == OOMKilled` plus namespace
     events;
   - target URL: `https://glasslab.org/exam/`, run from the provisioner with
     `glasslab.org` resolved to the gateway's LAN IP `192.168.1.4`. On 2026-10-20
     the router's hairpin path to the public IP lost about 1 in 15 new connections.
   Pass criteria are Mike's: all 20 ready ≤ 120 s, synchronized pass ≤ 60 s,
   memory and disk headroom ≥ 20 %, no OOM. Also compare with node02's 66.9 s /
   28.7 s, and treat a large regression as a finding even if the targets pass.
5. **Hub restart with live sessions:** start 5 sessions, delete the hub pod, and
   check that the hub comes back and that the existing servers and kernels are
   reachable again. Running student pods are left alone, and JupyterHub re-adds
   proxy routes from its DB at startup.
6. **Node loss drill:** drain one exam node with student sessions on it. Expected:
   those students restart their server from the hub page and land on another exam
   node with their saved work intact; unsaved kernel state is lost. Record the
   recovery time.
7. **Backup and restore rehearsal:** checkpoint 1 and the final backup, then a
   restore into `glasslab-exam-restore`, validated with Mike's verify scripts (§5.3).
8. **Account flow:** register the roster with signup open, log in as each account,
   close signup (404), and check the `RosterAuthenticator` rejection for a name not
   on the roster.

## 8. Cutover and rollback

### 8.1 Timeline for the first cluster-hosted exam

| When | Step | Gate |
|---|---|---|
| T-21 d | Decisions in §9 settled; hub image with KubeSpawner built and published; manifests reviewed and merged to `testing` | PR review |
| T-14 d | Rehearsal deployment in `glasslab-exam` with a test roster; acceptance tests 1–8 | All pass, or a dated fix list |
| T-7 d | Go/no-go #1. If no-go, start borrowing a node for the Docker path (`node02-runbook.md`), which takes hours of node work plus a load test | Maintainer decision |
| T-3 d | Real exam deployment: fresh hub state, per-exam NFS directory, real roster; exam nodes labelled, tainted and drained; images pre-pulled; acceptance tests 2, 3, 4 and 8 re-run | All pass |
| T-2 d → T-1 d | Accounts registered, signup closed, backup checkpoint 1; gateway route switched to the NodePort and tested from campus | Smoke through the public URL |
| Exam | Rollout freeze on the cluster; no changes in `glasslab-exam`; operator watching pods and events | — |
| T+0 | Final backup; grading export; NodePort Service removed or hub scaled to 0 | Restore spot-check |
| T+1 d | Exam nodes untainted, kubelet config restored, nodes back in general use | `kubectl get nodes` Ready, research pods scheduling |

### 8.2 Rollback

- **Before the exam** (any gate fails): point the gateway route back at the Docker
  host. That means restoring `exam-route.conf` from its `.bak-<stamp>`, then
  `apache2ctl configtest` and a reload. The Docker host has to have been prepared
  in parallel from T-7 d. Its accounts are registered separately, because hub
  state does not move between the two deployments.
- **During the exam:**
  - hub pod crash: Kubernetes restarts it on the same node. State is on the local
    PV and running student pods continue (acceptance test 5).
  - student node loss: affected students restart their server (acceptance test 6).
  - hub node loss: the outage lasts until the node returns, or until checkpoint 1
    is restored to a local PV on another exam node and the gateway route is
    switched to that node. Student work is on the NAS and unaffected. Rehearse this
    once and record the time. It is the main availability gap, and the same one
    node02 had.
  - NAS loss: student saves fail until it returns. The 15-minute CronJob limits the
    data at risk. This failure mode is new compared with node02's local disk.
- **After the exam:** nothing in the research namespaces changes, so rolling back
  is just deleting `glasslab-exam` once the backups are verified.

## 9. Open decisions and things not checked live

Decisions:

1. Option A (Mike's image + KubeSpawner) or Option B (Z2JH 4.4.0). This plan
   recommends A.
2. Who builds and publishes the K8s hub image, and from which source of truth.
3. A dedicated Synology share/export for exam data, or a subdirectory of
   `/volume1/backup`.
4. Which workers are exam nodes, and whether a cluster-wide PID limit is
   acceptable instead of exam-only nodes.
5. Backup target: Glasslab MinIO, off-site, or both.
6. Whether the gateway allow list keeps `192.168.0.0/16`
   ([ccny-glasslab/glasslab-cluster-config#696](https://github.com/ccny-glasslab/glasslab-cluster-config/pull/696)),
   which also admits LAN hosts.

Checked live during the 2026-10-20 build:

- Allocatable: node01 and node02 8 CPU / ~63 GiB, node03 and node04 4 CPU / ~31 GiB, node05
  8 CPU / ~31 GiB, cp01 4 CPU. `archimedes` was NotReady since 2026-08-24 and its Node object was
  deleted on 2026-10-19.
- `.207` export: root is **not** squashed; every non-root UID is (NFSv4.1 and NFSv3 both). Decision 3
  therefore also needs the export set to pass UIDs through, or a dedicated share that does.
- `metrics-server` is not installed (`kubectl top` reports no Metrics API).
- NodePort 30800 is now in use by `glasslab-exam/proxy-public`.

Still not checked: Calico/Felix `defaultEndpointToHostAction` (a student pod's own-node traffic was
not probed separately), kube-proxy mode, and NAS free capacity.

## 10. Effort estimate

| Phase | Work | Estimate |
|---|---|---|
| 0 | Live inventory (above), §9 decisions | 0.5 day |
| 1 | Hub image: requirements, lock, K8s config, build, digest manifest, GHCR publish | 1–1.5 days |
| 2 | Manifests under `kubeadm/glasslab-exam/`: namespace and PSA, RBAC, VAP, PVs/PVCs, prep Job, hub Deployment and Services, NetworkPolicies, pre-puller; Ansible task for the exam-node kubelet config | 1.5–2 days |
| 3 | `exam-netpol-check.sh`; `load-test-k8s.sh` + sampling changes to `make-load-exam.py` | 1.5 days |
| 4 | Rehearsal on the live cluster: acceptance tests 1–8, fixes | 1.5–2 days |
| 5 | Backup/restore Job, CronJob, MinIO user and policy, restore rehearsal | 1 day |
| 6 | Runbook (`runbooks/exam-jupyterhub.md`), the exposure-doc exception, gateway route change procedure | 0.5 day |
| | **Total** | **~7.5–9 engineer-days**, about 3 calendar weeks with reviews and one full rehearsal |

A local rehearsal on kind with Calico can do most of Phases 2 and 3 before
touching the live cluster: policy matrix, PSA, VAP and RBAC. The load test,
NodePort source-IP behaviour, NFS and kubelet PID limit need the real cluster.

Recommended order: Phases 0–3 well before the next exam, Phase 4 no later than
T-14 d, and the node02 Docker path kept ready as the fallback until one exam has
run cleanly on the cluster.
