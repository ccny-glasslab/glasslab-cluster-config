# Exam JupyterHub on Kubernetes (live since 2026-10-20 17:46)

`https://glasslab.org/exam/` → gateway Apache (campus-only) → `node03:30800` (NodePort
`proxy-public`) → Zero to JupyterHub 4.4.0 release `exam` in namespace `glasslab-exam`.
Mike's student image (`exam-student:release`) runs unchanged; his hub config is ported
into `values.yaml`.

| Piece | Where |
|---|---|
| Hub + proxy pods | node03 (hub DB: PV `glasslab-exam-hub-db`, hostPath `/var/lib/glasslab-exam/hub`) |
| Student pods + files | names ending in an odd number on **node01**, even on **node05**; files in `/var/lib/glasslab-exam/users/<name>` on that node, mounted as `~/work` |
| Student image | since 2026-10-27 `exam-student:csci1910` (CSCI1910F26 midterm: `release` + CPU PyTorch 2.11.0 + Part C notebooks + 94 datasets, config `sha256:53177a8b…`), imported into containerd on node01, node03, node04, node05 and held by `image-keeper-node01`/`-node05` pods (`~/exam-k8s/csci1910/image-keeper.yaml` on the provisioner) |
| Accounts | 23-account CSCI1910 roster, `michael.grossberg77` is hub admin. Student names stay out of this public repo: the real `allowed_users` list is only in the live `~gr66ss-glasslab/exam-k8s/values.yaml`, and passwords only in `exam-k8s/private/` (0700), on the provisioner; signup closed |
| Fallback | node02 has rejoined the cluster (2026-10-26); there is no Docker fallback any more |

Why local disks: on 2026-10-20 the .207 NFS export refused writes from non-root users (even
into a directory listed as `0777`), and students must not run as root.

Later the same day a dedicated share, `/volume1/glasslab-jupyter`, was created that UID 1000
can write to ("No mapping" plus a plain-Unix `0755` share root; UID 1000/1001 probe passed).
See the NAS exports section of
[`network-storage-integration.md`](../../docs/glasslab-v2/network-storage-integration.md).
This exam stays on local disks. NFS is for later exams and researcher hubs.

## Tests passed before the switch (2026-10-20)

- Full student session (login, spawn, JupyterLab, kernel over WebSocket, save/delete): via the
  NodePort as student23 (node01) and through the public URL as student22 (node05) after the switch.
- Pod security: uid 1000, all capabilities dropped, no privilege escalation, seccomp
  RuntimeDefault, no service-account token, 1 CPU / 1 GiB limits.
- Network isolation (`netpol-probe.sh`, in this directory): student pods reach only the hub API and the proxy's
  public port. Everything else is blocked: other students, the proxy admin API, the Kubernetes API,
  cluster and external DNS, the research Postgres, the nodes, the provisioner, the gateway, node02,
  the internet, metadata. A control pod without student labels reached the internet, the API, DNS
  and the nodes, so the probes detect openings.
- 20-student load test (Mike's workload, `exam-bundle/labbox/load-test-k8s.sh`): all ready in 43.7 s
  (target ≤ 120), synchronized slowest 15.6 s (≤ 60), staggered 12.6 s. Node memory ≥ 85 % free,
  no swap, no OOM kills. Faster than node02 (66.9 s / 28.7 s) because students span 16 cores.

## Operating it

From the provisioner (`K="sudo -n env KUBECONFIG=/home/glasslab/.kube/config kubectl"`):

```bash
$K -n glasslab-exam get pods -o wide            # hub, proxy, jupyter-<student>
$K -n glasslab-exam logs deploy/hub --tail=50
$K -n glasslab-exam delete pod jupyter-student07   # force-stop one student's server (files stay)
```

Password resets: log in as the hub admin (`michael.grossberg77`) at `https://glasslab.org/exam/hub/authorize` (NativeAuthenticator)
or the change-password page. Spares: student01–05.

**Switch back to node02** (gateway): `sudo bash ~/switch-exam-route.sh node02` (source: `exam-bundle/gateway/switch-exam-route.sh`). Switching to
Kubernetes again: `... k8s`. Each switch backs up the route file, configtests and reloads.
Accounts on the two hubs are separate databases with the same passwords; student files are
**not** shared, so switch mid-exam only as a last resort.

Re-apply the chart (from `~/exam-k8s` on the provisioner):
`sudo -n env KUBECONFIG=/home/glasslab/.kube/config ~/bin/helm upgrade exam jupyterhub-4.4.0.tgz -n glasslab-exam -f values.yaml`
(never with `enable_signup=true` once students have accounts).

## Known differences from Mike's single-host design

- No per-container 256-process cap: Kubernetes has no per-pod PID limit without a kubelet setting.
- Students can reach the proxy's public port (the chart always allows it): the same login-protected
  site their browser uses.
- `node03:30800` (and every node's 30800) is reachable from the whole lab LAN, not only the gateway.
- Hub database and student files live on single nodes (node03; node01/node05). If one dies mid-exam,
  its students lose access until it returns, the same exposure node02 had.

## Switching to another course's image and roster

[`tools/switch-course.sh`](tools/switch-course.sh) is what switched the hub from the DSEI exam to
the CSCI1910 midterm on 2026-10-27 in about four minutes. It runs on the provisioner from
`~/exam-k8s` and needs no node SSH:

1. imports the image tar into containerd on node01/03/04/05 through short-lived privileged pods
   (`nsenter -t 1 -m -- ctr -n k8s.io images import -`) and checks the image ID on each node;
2. creates the new students' folders (and moves old student01–05 folders aside);
3. re-pins the image-keeper pods to the new tag (node05 garbage-collected the image under disk
   pressure on 2026-10-20);
4. `helm upgrade` with signup open, `tools/roster-sync-admin.py` (signs up or resets every
   roster account, logging in as the hub admin), `helm upgrade` with signup closed;
5. `tools/verify-logins.py` for every account.

Before running it, edit the provisioner's `~/exam-k8s/values.yaml` (`allowed_users`, `admin_users`,
`singleuser.image.tag`; never commit real student names) and the image name/ID at the top of the script, and put the roster
(`username,password`, no header) in `private/`. Then run `tools/student-session.py` as one odd
and one even student. With `delete_invalid_users: true`, accounts that leave the roster are removed
from the hub database on restart; their folders stay on disk.

## After the exam

Back up `/var/lib/glasslab-exam/users` from node01 **and** node05, then:
`helm uninstall exam -n glasslab-exam`, delete PV `glasslab-exam-hub-db` and the namespace,
and remove `/var/lib/glasslab-exam` on node01, node03 and node05 once the backup is verified.
