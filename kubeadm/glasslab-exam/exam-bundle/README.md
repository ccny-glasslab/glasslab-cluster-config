# Exam JupyterHub on node02 (Docker): the fallback

> **Since 2026-10-20 17:46 the live hub is the Kubernetes release** described in
> [`../README.md`](../README.md). This bundle is the node02 Docker deployment that
> served `/exam/` before that and stays running, untouched, as the fallback until the
> exam is over. Switch back with `exam-bundle/gateway/switch-exam-route.sh node02` on the
> gateway. The bundle lives at `~clusteradmin/exam` on node02 with this layout.
> The status, steps and test notes below are the node02 record as of 2026-10-20 15:45.

Mike's exam release (`exam-hub:release`, `exam-student:release`, images unchanged)
on node02, borrowed from the cluster, behind the gateway's existing glasslab.org
HTTPS site. Same pattern as `/dsei102/`: no new DNS name or certificate.

```
CCNY lab browser ──HTTPS──▶ gateway Apache, glasslab-ssl.conf (Let's Encrypt, acme-dns)
                              /exam/ → campus IPs only, TLS ends here
                            ──HTTP, lab LAN──▶ node02 192.168.1.11:8000/exam/ (hub)
                                               └─ exam-students bridge (br-exam), one container per student
```

## Status (2026-10-20, 15:45): node02 record

| Piece | State |
|---|---|
| Hub | **Live at https://glasslab.org/exam/** on node02. 26 accounts registered and verified, signup closed (404). Passwords: `/opt/exam-release/private/credentials.csv` on node02 (root-only). |
| node02 | Out of the cluster (cordoned, kubelet disabled, `NotReady`). Docker 29.1.3 + compose 2.40.3, 40 GB exam filesystem at `/srv/exam`, `exam-firewall` service enabled. |
| Firewall | Static checks and all live probes pass on node02. Port 8000 answers the gateway only (provisioner times out). |
| Gateway | `/exam/` route live since 2026-10-19 17:38 (backups `*.bak-20261019-173752`); PR [#699](https://github.com/ccny-glasslab/glasslab-cluster-config/pull/699) records it. |
| End-to-end | Full student sessions passed on node02 (student25, local port) and through the public URL (student24, from node02 via the gateway). |
| Load test (2026-10-20 16:33) | **Pass**, 20 students on node02 (Mike's workload, `labbox/load-test.sh`): all 20 servers ready in 66.9 s (target ≤ 120; spawns queue 5 at a time), synchronized pass slowest 28.7 s / median 27.9 s (target ≤ 60), staggered 27.5 s, memory headroom ≥ 88.6 %, disk ≥ 94.8 %, no swap, no OOM. Test notebooks removed and servers stopped afterwards. |
| Access | Temporary personal admin on node02: `ssh glasslab-node02` (gr66ss-glasslab, passwordless sudo). Remove after the exam (runbook Phase 3). |
| archimedes | Node object deleted (spec saved outside the repo); still in the Ansible inventory |

After any node02 reboot the hub does not start by itself (Mike's restart policy is off):
`sudo bash ~/exam/labbox/node02-setup.sh start` (starts the hub, then runs the live firewall probes).
Spot-check through the gateway any time: `sudo bash ~/exam/labbox/smoke-public.sh student24`.

## Files

| File | Goes to | Purpose |
|---|---|---|
| `exam-route.conf` (tracked in `live-config/gateway/`, PR #699) | gateway `/etc/apache2/glasslab/` | `/exam/` reverse proxy with WebSockets and the campus-only allow list, included from the 443 vhost |
| `exam-route-http.conf` (tracked in `live-config/gateway/`, PR #699) | gateway `/etc/apache2/glasslab/` | Port 80: `/exam` → HTTPS |
| `labbox/node02-setup.sh` | run on node02 | Staged setup: `stage1` (kubelet off, reboot), `stage2` (Docker, disk, firewall, release, images), `accounts`, `start` |
| `labbox/compose.lan.yaml` | release root | Hub at `bind_url …:8000/exam/` (startup option, image unchanged); LAN port for the gateway; fixed hub IP |
| `labbox/start-lab.sh` | release `scripts/` | Mike's `start-production.sh` with `compose.lan.yaml` in place of Caddy |
| `labbox/exam-firewall*.sh`, `.service` | `/opt/exam-firewall/`, systemd | Student-container isolation (below) |
| `tests/student-session.py` | anywhere with Python 3 | End-to-end check for one student: login, spawn, Lab, kernel over WebSocket, save |
| `node02-runbook.md` | — | Borrowing node02 and giving it back |

## Remaining steps

**node02** (needs clusteradmin's sudo password), from `~/exam`:
```bash
sudo bash ~/exam/labbox/node02-setup.sh stage1     # disables kubelet, reboots
sudo bash ~/exam/labbox/node02-setup.sh stage2
sudo bash ~/exam/labbox/node02-setup.sh accounts   # ends with a spare-account smoke test and firewall probes
```

**Gateway** (gr66ss-glasslab has sudo there):
```bash
sudo install -d /etc/apache2/glasslab
sudo install -m 0644 exam-route.conf exam-route-http.conf /etc/apache2/glasslab/
sudo sed -i 's/LABBOX_IP/192.168.1.11/g' /etc/apache2/glasslab/exam-route.conf
sudo cp -a /etc/apache2/sites-available/glasslab-ssl.conf{,.bak-$(date +%Y%m%d-%H%M)}
sudo cp -a /etc/apache2/sites-available/000-default.conf{,.bak-$(date +%Y%m%d-%H%M)}
sudo sed -i '0,/<\/Directory>/s//&\n        Include \/etc\/apache2\/glasslab\/exam-route.conf/' /etc/apache2/sites-available/glasslab-ssl.conf
sudo sed -i '0,/<\/Directory>/s//&\n        Include \/etc\/apache2\/glasslab\/exam-route-http.conf/' /etc/apache2/sites-available/000-default.conf
sudo a2enmod headers && sudo apache2ctl configtest && sudo systemctl reload apache2
```
The `Include` goes right after the `/dsei102/` `</Directory>`, ahead of any future `ProxyPass /`.
Rollback: restore the two `.bak-*` files, `apache2ctl configtest`, reload. Then snapshot the gateway
config into the repo (`scripts/snapshot-gateway-config.sh`), open a PR to `testing`, and update `docs/gateway-web.md`.

**Check from a campus machine:** `python3 -I tests/student-session.py https://glasslab.org/exam student24 '<password>'`,
then a real browser in a CCNY lab.

## Student network policy (node02 host firewall)

| From → to | Result |
|---|---|
| student → hub API `172.30.99.10:8081` | allowed |
| hub → student server `:8888` | allowed |
| student → another student, hub `:8000`, the host, LAN, internet, metadata, DNS | dropped |
| anyone but the gateway (`192.168.1.4`) → `192.168.1.11:8000` | dropped |
| all IPv6 on `br-exam` | dropped |

Own chains (`EXAM-FWD`, `EXAM-IN`) jumped first from `DOCKER-USER` and `INPUT`; needs Docker's
iptables backend and br_netfilter (both handled by the apply script; node02 already has br_netfilter).

## What was tested (2026-10-19, locally)

- **Real images, real config, under `/exam/`:** Mike's `compose.yaml` + `compose.lan.yaml` on Docker 29, containerd image store:
  - Fresh account flow: 26 registered, 26 logins verified, signup 404 once closed.
  - Full student session: login, spawn (3.6 s), JupyterLab, kernel over WebSocket running numpy/pandas/sklearn/matplotlib (NumPy 2.5.4), notebook save/delete.
- **Through the gateway route:** Ubuntu 22.04 Apache 2.4.52 with the gateway's live `000-default.conf`
  and `glasslab-ssl.conf`, edited exactly as above, in front of that hub:
  - the same student session passed over HTTPS at `https://glasslab.org/exam/`;
  - landing page and `/dsei102/` unchanged;
  - `/exam` → `/exam/`, and plain HTTP → HTTPS;
  - non-campus source → 403.
- **Firewall** in Docker 29 dind with stand-in images:
  - without rules, peer, hub:8000 and host were reachable (Mike's A13);
  - with rules, every probe passed and port 8000 answered only the "gateway";
  - applying twice is idempotent, and remove is clean.
- **Image check** in `node02-setup.sh`: all four images OK against the tarball and manifest.

## Not tested

- Anything on node02 or the real gateway (nothing applied there yet), a CCNY lab browser,
  Brightspace, 20-user load on node02, reboot persistence of the firewall unit.
- The hub shares an origin with the glasslab.org landing page and `/dsei102/` (static, admin-controlled);
  students already share one origin among themselves (Mike's accepted single-host limitation).
- The gateway → node02 hop is plain HTTP on the lab LAN, so passwords cross it unencrypted.
