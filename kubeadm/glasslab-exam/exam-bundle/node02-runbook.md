# Borrowing node02 as the exam host (DRAFT, nothing done yet)

node02 (`192.168.1.11`, 8 cores, 62 GiB, Ubuntu 24.04) leaves Kubernetes for the
exam week, runs Mike's Docker stack, then rejoins. Kubernetes is stopped rather
than reset: the node object, its certificates and its Calico address block stay
in place, so rejoining is "start kubelet, uncordon".

`LABBOX_IP` / `EXAM_LAN_IP` in the other drafts = `192.168.1.11`.

## What was checked (read-only, 2026-10-19)

| Item | Finding |
|---|---|
| Pods on node02 | 4 agents + `schedule-worker` (glasslab-v2), one `calico-typha`, DaemonSets (calico-node, csi-node-driver, kube-proxy, nvidia-device-plugin). Requests total 350m CPU / 512 Mi. |
| Volumes / pinning | No PV has node02 affinity; no Deployment/StatefulSet/CronJob selects node02. (`kubeadm/glasslab-v2/gpu-runner` pins node02 but is not deployed.) |
| Typha | 3 replicas, PDB allows 1 unavailable, 4 other schedulable nodes: drain can move it. |
| GPU | RTX A4000 advertised, but `nvidia-smi` fails with "Driver/library version mismatch" (47 days since reboot). 0 GPU jobs in the last 7 days. |
| Docker | `apt-get -s install docker.io` → 29.1.3 from noble-updates, 6 new packages, **0 removed**, keeps containerd 2.2.1 (Ubuntu). Not Docker's `docker-ce`, which would replace containerd. |
| Disk | One 466 GB disk, 265 GB free on `/`. No spare disk for `/srv/exam`. |
| Kubelet client cert | Issued 2026-03-10; expiry not readable without root (kubeadm default: 1 year, auto-rotated). |
| Firewall | `ufw` installed and enabled at boot; rule state not readable without root. br_netfilter on, `ip_forward=1`. |
| Access | `clusteradmin` via the provisioner. Full `sudo` **needs clusteradmin's password**; only the maintenance wrappers (including `glasslab-reboot-node`) are passwordless. |

## Before starting

- Someone with clusteradmin's sudo password (or infrastructure_admin) runs the node steps.
- Tell the lab that node02 and its GPU are out from Tuesday morning until after the exam.
  GPU jobs that need the A4000 will wait as Pending.
- Lab machine checks from the other drafts are ready (gateway conf, firewall, compose override).

Shell prefixes: `[prov]` = provisioner, as
`sudo -n env KUBECONFIG=/home/glasslab/.kube/config kubectl ...`; `[node02]` = node02 as clusteradmin.

## Phase 1 — take node02 out (Tuesday morning)

1. `[prov]` Record the starting state:
   `kubectl get pods -A -o wide --field-selector spec.nodeName=node02 > node02-before.txt`
2. `[prov]` `kubectl cordon node02`
3. `[prov]` `kubectl annotate node node02 glasslab.org/borrowed='exam host 2026-10-20..2026-10-23, see exam runbook'`
4. `[prov]` `kubectl drain node02 --ignore-daemonsets --delete-emptydir-data --timeout=10m`
5. `[prov]` Verify the agents and schedule-worker are Running on other nodes, and typha is 3/3:
   `kubectl -n glasslab-v2 get pods -o wide`, `kubectl -n calico-system get deploy calico-typha`
   **Done 2026-10-19 17:10–17:13 (steps 1–5).** All four glasslab-v2 deployments rolled out
   elsewhere (design → node01, intake → node03, interpretation and schedule-worker → node04).
   Saved state, and the specs of the two old `debug-triplet-*` sleep pods the drain deleted
   (empty logs), are in `state/`. **Typha:** the replacement replica was bound to
   `archimedes`, which tolerates every taint and has been unreachable since Aug 24. So it's
   Pending: typha runs 2/3 and its PDB allows 0 more disruptions. Calico is not degraded.
   A cordon doesn't keep typha off archimedes because typha tolerates the unschedulable taint;
   archimedes needs a decision of its own.
   **17:17:** archimedes' node object was deleted at the user's request (spec saved in
   `state/archimedes-node.yaml`; if its kubelet ever comes back with valid credentials it
   re-registers itself). Typha then went 3/3, but the new replica landed back on cordoned
   node02. Expect 2/3 (PDB 0) once kubelet stops in step 6. Calico runs fine on 2, and it
   heals when node02 rejoins. archimedes is still in `ansible/inventory/hosts.yml`.
6. `[node02]` `sudo systemctl disable --now kubelet`
7. `[node02]` `sudo /usr/local/sbin/glasslab-reboot-node`. With kubelet off, nothing restarts
   calico-node or kube-proxy, and their iptables rules are not persistent. The reboot
   should also clear the NVIDIA driver mismatch.
8. `[node02]` Verify a clean network stack:
   `sudo iptables-save | grep -cE 'cali-|KUBE-'` → `0`; `systemctl is-enabled kubelet` → `disabled`
9. `[prov]` node02 shows `NotReady,SchedulingDisabled`. Its DaemonSet pods show
   Unknown/Terminating. That's expected.

**Rollback from any step in this phase:** `sudo systemctl enable --now kubelet`, then
`kubectl uncordon node02`.

## Phase 2 — exam host (Tuesday)

10. `[node02]` `sudo apt-get install docker.io`. Check `docker info --format '{{.FirewallBackend.Driver}}'` → `iptables`.
    Don't add anyone to the `docker` group; it's root-equivalent.
11. `[node02]` A dedicated, size-capped exam filesystem. This gives Mike's start script the mountpoint
    and UUID it requires, and keeps a full student disk from filling `/`:
    ```bash
    sudo fallocate -l 40G /var/lib/exam-data.img
    sudo mkfs.ext4 -L examdata /var/lib/exam-data.img
    sudo mkdir -p /srv/exam
    echo '/var/lib/exam-data.img /srv/exam ext4 loop,nofail 0 2' | sudo tee -a /etc/fstab
    sudo mount /srv/exam && findmnt -n -o UUID /srv/exam   # -> EXAM_DISK_UUID
    ```
12. `[node02]` `sudo ufw status verbose`. Published Docker ports bypass ufw's INPUT rules,
    and the exam firewall enforces gateway-only on port 8000. Record the state either way.
13. `[node02]` `node02-setup.sh stage2`, then `accounts` (README → "Remaining steps"), with `EXAM_LAN_IP=192.168.1.11`:
    images, release, firewall unit, storage, accounts, `start-lab.sh`, `--probe`.
14. Gateway: `README.md` → "Remaining steps" → Gateway (the `/exam/` route on the existing glasslab.org site).
15. Tests (README plus Mike's acceptance list): a CCNY lab browser end to end, a long-lived kernel,
    20-user load, and a reboot of node02. After the reboot, kubelet must stay disabled,
    Docker and `exam-firewall` must come up with the check passing, and the hub is started by hand.

## Phase 3 — after the exam (only once the instructor releases the data)

16. Stop student servers from `/hub/admin`, then stop the hub (Mike's README stop
    procedure, not `down -v`). Copy `/srv/exam` off the machine (the .207 NAS or Mike's restic
    flow) and verify the hashes. Retention and deletion need the instructor's sign-off (Mike's A22).
17. `[node02]` `sudo systemctl disable --now exam-firewall docker.service docker.socket`
    (leave containerd alone; Kubernetes uses it), then `sudo apt-get purge docker.io`.
    Remove `/etc/systemd/system/exam-firewall.service`, `/opt/exam-firewall`, `/etc/exam`.
17b. `[node02]` Remove the temporary personal admin account:
    `sudo rm -f /etc/sudoers.d/90-gr66ss-glasslab-exam && sudo userdel -r gr66ss-glasslab`,
    and the `Host glasslab-node02` block plus the `glasslab-node02` known_hosts line on the workstation.
18. `[node02]` After the backup is verified: `sudo umount /srv/exam`, remove the fstab line,
    `sudo rm /var/lib/exam-data.img`, `sudo rm -rf /var/lib/docker`.
19. `[node02]` `sudo /usr/local/sbin/glasslab-reboot-node`, then
    `sudo iptables-save | grep -cE 'DOCKER|EXAM'` → `0`.
20. `[node02]` `sudo systemctl enable --now kubelet`
21. `[prov]` Wait for `Ready`. Check that the calico-node, kube-proxy, csi-node-driver and nvidia-device-plugin
    pods on node02 are Running, then `kubectl uncordon node02` and
    `kubectl annotate node node02 glasslab.org/borrowed-`.
    Check `kubectl -n calico-system get deploy calico-typha` is 3/3. If a replica is still
    stuck on archimedes, resolve archimedes first, then delete that Pending pod.
22. `[prov]` `nvidia.com/gpu` allocatable is 1 on node02 and `nvidia-smi` works there; then run the repo's
    `./scripts/smoke-test-v2.sh` from the canonical checkout.

**If kubelet can't rejoin** (for example its client certificate expired or was rejected): on cp01,
`sudo kubeadm token create --print-join-command`; on node02, `sudo kubeadm reset -f`, remove
`/etc/cni/net.d/*`, then run that join command. This is the repo's documented bootstrap path
(`docs/kubernetes-bootstrap.md`). Re-run `ansible/playbooks/enable-gpu-node.yml` for node02 if
the GPU isn't advertised.

## Repo record

Open a short PR to `testing` noting node02's temporary removal (dates, reason,
this runbook), and another when it rejoins. Ansible playbooks targeting `workers` or
`gpu_nodes` should skip node02 meanwhile (`--limit '!node02'`).
