# Research Notebooks

Personal GPU Jupyter servers for researchers who edit in VS Code on their own
computer and run notebook kernels on a lab GPU. The researcher-facing steps
are in [docs/research-notebooks.md](../../../docs/research-notebooks.md).

This is a deliberate exception to the rule that GPU work goes through the
research orchestrator as bounded Jobs. Each notebook is one long-running pod,
owned by one researcher, with capped resources, no cluster credentials, and a
narrow network path.

```text
researcher's VS Code
  -> ssh -L 8888:192.168.1.11:30888 <user>@glasslab.org   (personal gateway login)
  -> gateway 192.168.1.4
  -> node02 NodePort 30888
  -> notebook pod (glasslab-notebooks namespace, RTX A4000)
```

`shradhagodse` can also connect directly from her lab-network computer
(`192.168.1.157`) to `http://192.168.1.11:30888`, without the tunnel.

## Current Notebooks

| Owner | Purpose | Node / GPU | NodePort | Home volume |
| --- | --- | --- | --- | --- |
| `shradhagodse` | FreightTG thesis (`ccny-glasslab/freighttg`) | `node02` RTX A4000 | 30888 | g-nas `/volume1/glasslab-jupyter/notebooks/shradhagodse`, 200Gi |

## Files

- `00-namespace.yaml`: `glasslab-notebooks` namespace (restricted Pod
  Security) and the token-less `research-notebook` ServiceAccount
- `20-storage-<owner>.yaml`: static NFS PV and PVC for the owner's home
- `30-secret.example.yaml`: documentation-only schema for the login token
- `40-deployment-<owner>.yaml`: the Jupyter Deployment and its NodePort Service
- `60-network-policy.yaml`: default-deny, plus ingress from the gateway, the
  node, and allowlisted researcher lab computers, and DNS plus internet HTTPS
  egress

## What A Notebook Can And Cannot Do

- It runs as UID 1000 with no Linux capabilities, no privilege escalation, and
  no Kubernetes ServiceAccount token.
- It accepts connections on 8888 only from the gateway (`192.168.1.4`),
  from node02 itself (kubelet probes), and from `shradhagodse`'s lab computer
  (`192.168.1.157`). Every other lab machine and pod is refused. That direct
  path is plain HTTP on the lab LAN, so the token crosses the LAN unencrypted;
  the tunnel does not have that exposure. `192.168.1.157` has no router
  reservation yet, so reserve it before relying on the allowlist.
- It can resolve DNS and open HTTPS (443) connections to the internet: GitHub,
  PyPI, conda, Hugging Face. It cannot reach the lab LAN, the CCNY campus
  range, the pod network, or the service network, so Postgres, MinIO,
  workflow-api, the exo hosts, and other lab machines are out of reach.
- Its Git and GitHub access is the researcher's own credential, stored in
  their home volume. The lab issues no GitHub token.
- Its priority is 0, lower than both Glasslab priority classes, so reviewed
  research workloads can preempt it.

## Browser Tools

Notebooks run the lab's own image, `ghcr.io/ccny-glasslab/glasslab-research-notebook`,
built from [`services/research-notebook/Dockerfile`](../../../services/research-notebook/Dockerfile)
on top of the pinned Docker Stacks PyTorch CUDA 12 image. It adds:

| Tool | Where | Package |
| --- | --- | --- |
| VS Code in the browser | `/vscode/` | code-server 4.140.0 via `jupyter-vscode-proxy` |
| TensorBoard (logs in `~/logs`) | `/tensorboard/` | `tensorboard`, `jupyter-tensorboard-proxy` |
| GPU dashboard | JupyterLab **GPU Dashboards** sidebar | `jupyterlab-nvdashboard` |
| CPU and memory meter | JupyterLab status bar | `jupyter-resource-usage` |
| Git panel | JupyterLab left sidebar | `jupyterlab-git` (from the base image) |

All of it is behind Jupyter's token login; no port or Service changes.
code-server runs with its own auth turned off, on a mode-0600 Unix socket that
only the `/vscode/` proxy reaches, and keeps its settings and extensions under
`~/.local/share/code-server` in the home volume. The code-server tarball is
checked against a pinned sha256, and the Python packages are installed against
the base image's own versions as constraints, so the build never upgrades
JupyterLab, Jupyter Server, or PyTorch. The build fails if any extension does
not load.

Merging a Dockerfile change to `main` runs **Publish Research Notebook Image**,
which pushes `glasslab-research-notebook:<full-commit-sha>` and prints its
digest in the run summary. Pin that `<sha>@<digest>` in
`40-deployment-<owner>.yaml` in a follow-up change, then apply it. The package
is private, so the namespace needs its own copy of the `glasslab-ghcr-pull`
secret (Deploy step 3).

RStudio is not included: it needs R and the root-installed `rstudio-server`
package, a larger addition to this image.

## Prerequisites

1. **A managed gateway account.** The researcher needs a personal gateway login
   that allows TCP forwarding (the gateway's sshd does). Add the account to
   `ansible/group_vars/identity_hosts.yml` first. Key-only hardening publishes
   `AllowUsers` from that ledger, so an account outside it loses gateway access
   the next time the hardening playbook runs.
2. **The GPU is free.** node02 has one A4000. `glasslab-gpu-runner` (glasslab-v2)
   and the faiss integration test Job also request it. Check before deploying:

   ```bash
   sudo -n env KUBECONFIG=/home/glasslab/.kube/config \
     kubectl describe node node02 | grep -A8 'Allocated resources'
   sudo -n env KUBECONFIG=/home/glasslab/.kube/config \
     kubectl -n glasslab-v2 get deploy glasslab-gpu-runner
   ```

   If the runner holds the GPU, the notebook stays `Pending` until someone
   decides which workload gets it. Scaling the runner to zero is a separate,
   explicit decision; this directory does not make it.
3. **The image is pullable on node02.** The lab notebook image is a private
   GHCR package of about 16 GB unpacked, so the namespace needs the
   `glasslab-ghcr-pull` secret (Deploy step 3), and the first pull on a node
   takes several minutes.

## Deploy A Notebook

Run from the canonical provisioner checkout after the change is merged. The
commands here and below use this shorthand; define it first in each shell:

```bash
K() { sudo -n env KUBECONFIG=/home/glasslab/.kube/config kubectl "$@"; }
```

1. Create the home directory on g-nas, owned by the image's `jovyan:users`
   (1000:100). Use the **`glasslab-jupyter`** export: the older `backup`
   export carries a Synology ACL that refuses UID 1000 even after
   `chown 1000:100` + `chmod 700` (verified 2026-10-27).

   ```bash
   sudo mkdir -p /mnt/gnas-jupyter
   sudo mount -t nfs -o nfsvers=4.1 192.168.1.207:/volume1/glasslab-jupyter /mnt/gnas-jupyter
   sudo mkdir -p /mnt/gnas-jupyter/notebooks/shradhagodse
   sudo chown 1000:100 /mnt/gnas-jupyter/notebooks/shradhagodse
   sudo chmod 700 /mnt/gnas-jupyter/notebooks/shradhagodse
   sudo umount /mnt/gnas-jupyter
   ```

   `glasslab-jupyter`'s root is plain Unix mode, not a DSM ACL, so UID 1000 can
   write. Do not edit that share's permissions in DSM (it can restore the ACL).

2. Apply the namespace, storage, and network policy:

   ```bash
   cd /home/glasslab/cluster-config/kubeadm/glasslab-v2/research-notebooks
   K apply -f 00-namespace.yaml
   K apply -f 20-storage-shradhagodse.yaml
   K apply -f 60-network-policy.yaml
   K -n glasslab-notebooks get pvc notebook-shradhagodse-home   # expect Bound
   ```

3. Create the login token. The `$(...)` keeps the value out of shell history:

   ```bash
   K -n glasslab-notebooks create secret generic notebook-shradhagodse-token \
     --from-literal=JUPYTER_TOKEN="$(openssl rand -hex 32)"
   ```

   Copy the GHCR pull secret from `glasslab-v2`, since the notebook image is a
   private package. Only the credential is copied, never printed:

   ```bash
   K -n glasslab-v2 get secret glasslab-ghcr-pull -o json \
     | jq '{apiVersion, kind, type, data, metadata: {name: .metadata.name, namespace: "glasslab-notebooks"}}' \
     | K apply -f -
   ```

4. Apply the notebook and wait for it:

   ```bash
   K apply -f 35-configmap-tools.yaml
   K apply -f 40-deployment-shradhagodse.yaml
   K -n glasslab-notebooks rollout status deploy/notebook-shradhagodse --timeout=15m
   K -n glasslab-notebooks exec deploy/notebook-shradhagodse -- \
     python -c 'import torch; print(torch.cuda.get_device_name(0))'
   ```

5. Check the network path from the gateway. Expect a JSON version string from
   the first command and a timeout from the second (the provisioner is not
   allowed in):

   ```bash
   ssh glasslab-gateway 'curl -s -m 5 http://192.168.1.11:30888/api'
   curl -s -m 5 http://192.168.1.11:30888/api || echo "refused as expected"
   ```

6. Give the researcher the token through a private channel, never in an issue,
   pull request, or shared chat:

   ```bash
   K -n glasslab-notebooks get secret notebook-shradhagodse-token \
     -o jsonpath='{.data.JUPYTER_TOKEN}' | base64 -d; echo
   ```

## Day-To-Day Operations

Pause a notebook to free the GPU. The home volume and token are kept:

```bash
K -n glasslab-notebooks scale deploy/notebook-shradhagodse --replicas=0
K -n glasslab-notebooks scale deploy/notebook-shradhagodse --replicas=1
```

Rotate the token, for example after it was shared by mistake:

```bash
K -n glasslab-notebooks delete secret notebook-shradhagodse-token
K -n glasslab-notebooks create secret generic notebook-shradhagodse-token \
  --from-literal=JUPYTER_TOKEN="$(openssl rand -hex 32)"
K -n glasslab-notebooks rollout restart deploy/notebook-shradhagodse
```

Look at a notebook without logging into it:

```bash
K -n glasslab-notebooks get pods -o wide
K -n glasslab-notebooks logs deploy/notebook-shradhagodse --tail=50
```

## Retire A Notebook

```bash
K delete -f 40-deployment-shradhagodse.yaml
K -n glasslab-notebooks delete secret notebook-shradhagodse-token
K delete -f 20-storage-shradhagodse.yaml
```

The PV's `Retain` policy leaves the files on g-nas. Archive or delete the
directory there as a separate decision. Ask the researcher to revoke the GitHub
token they used.

## Adding Another Researcher

Copy `20-storage-shradhagodse.yaml` and `40-deployment-shradhagodse.yaml` with
the new owner's name in every `shradhagodse` position, pick an unused NodePort,
and add a row to the table above. The namespace and network policy are shared.
A notebook on another GPU node also needs that node's /32 in the ingress policy
for its probes.
