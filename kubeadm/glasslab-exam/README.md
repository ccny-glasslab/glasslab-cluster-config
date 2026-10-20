# Exam JupyterHub on Kubernetes (live since 2026-10-20 17:46)

`https://glasslab.org/exam/` → gateway Apache (campus-only) → `node03:30800` (NodePort
`proxy-public`) → Zero to JupyterHub 4.4.0 release `exam` in namespace `glasslab-exam`.
Mike's student image (`exam-student:release`) runs unchanged; his hub config is ported
into `values.yaml`.

| Piece | Where |
|---|---|
| Hub + proxy pods | node03 (hub DB: PV `glasslab-exam-hub-db`, hostPath `/var/lib/glasslab-exam/hub`) |
| Student pods + files | odd students and `examadmin` on **node01**, even on **node05**; files in `/var/lib/glasslab-exam/users/<name>` on that node, mounted as `~/work` |
| Student image | imported into containerd on node01, node03, node04, node05 (`docker.io/library/exam-student:release`, manifest `ddf97c44…`) |
| Accounts | the same 26 roster accounts and passwords as node02 (`/opt/exam-release/private/credentials.csv` on node02, root-only); signup closed |
| Fallback | node02 Docker hub, still running and untouched |

Why local disks: the .207 NFS export refuses writes from non-root users (even into a
`0777` directory), and students must not run as root.

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

Password resets: log in as `examadmin` at `https://glasslab.org/exam/hub/authorize` (NativeAuthenticator)
or the change-password page. Spares: student21–25.

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

## After the exam

Back up `/var/lib/glasslab-exam/users` from node01 **and** node05, then:
`helm uninstall exam -n glasslab-exam`, delete PV `glasslab-exam-hub-db` and the namespace,
and remove `/var/lib/glasslab-exam` on node01, node03 and node05 once the backup is verified.
