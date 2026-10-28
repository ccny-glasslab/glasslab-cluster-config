# Using A Lab GPU Notebook From VS Code

This guide is for a researcher who has been given a Glasslab GPU notebook. You
keep editing in VS Code on your own computer, as with Google Colab, but the
notebook kernel runs on the lab's NVIDIA RTX A4000 and your files stay on a lab
disk between sessions. Administrators deploy and run notebooks from
[kubeadm/glasslab-v2/research-notebooks](../kubeadm/glasslab-v2/research-notebooks/README.md).

You need:

- your Glasslab gateway login (for example `shradhagodse@glasslab.org`) with
  your SSH key
- the notebook token an administrator sent you privately
- VS Code with the **Jupyter** extension
- your own GitHub account with access to the project repository

## 1. Open The Tunnel

The notebook is not on the internet. You reach it through the gateway with an
SSH tunnel. In a terminal (PowerShell on Windows, Terminal on a Mac):

```bash
ssh -N -L 8888:192.168.1.11:30888 shradhagodse@glasslab.org
```

The command prints nothing and keeps running. Leave that window open while you
work; closing it disconnects VS Code from the notebook.

To shorten it, add this to `~/.ssh/config` (on Windows,
`C:\Users\<you>\.ssh\config`) and then run `ssh -N glasslab-notebook`:

```text
Host glasslab-notebook
  HostName glasslab.org
  User shradhagodse
  LocalForward 8888 192.168.1.11:30888
```

If port 8888 is already in use on your computer, use another local port, such
as `-L 8899:192.168.1.11:30888`, and use that port below.

**From your lab computer** (`192.168.1.157`, on the lab network) you can skip
the tunnel and use `http://192.168.1.11:30888` wherever this guide says
`http://localhost:8888`. Only that address is allowed in. This connection is
not encrypted. From any other computer, use the tunnel.

## 2. Set Up Once In JupyterLab

Open `http://localhost:8888/lab` in a browser and enter your token. Use
**File > New > Terminal** for the steps below. Everything in your home folder
(`/home/jovyan`) is kept; anything outside it is reset when the notebook
restarts.

Check the GPU:

```bash
python -c 'import torch; print(torch.cuda.get_device_name(0))'
```

Connect Git to GitHub with your own credentials. Create a **fine-grained**
personal access token at <https://github.com/settings/personal-access-tokens>
with resource owner `ccny-glasslab`, access to the `freighttg` repository only,
**Contents: Read and write** permission, and an expiry of 90 days or less. The
organization may need to approve it. Then:

```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"   # the email on your GitHub account
git config --global credential.helper store
git clone https://github.com/ccny-glasslab/freighttg.git ~/freighttg
```

When Git asks, enter your GitHub username, and paste the token as the
password. Git saves it in `~/.git-credentials`, readable only by you inside the
notebook. Lab storage administrators can technically read that disk, so use a
token scoped to this one repository, and revoke it on GitHub when you finish.
If you prefer not to save it, use
`git config --global credential.helper 'cache --timeout=43200'` instead, and
paste the token once per day.

Create a Python environment for the project. `--system-site-packages` keeps
the GPU build of PyTorch that comes with the notebook:

```bash
python -m venv --system-site-packages ~/venvs/freighttg
~/venvs/freighttg/bin/pip install -e ~/freighttg
~/venvs/freighttg/bin/python -m ipykernel install --user --name freighttg \
  --display-name "Python (freighttg)"
```

Bring your data over. Small files can be dragged into the JupyterLab file
browser. Large files are faster to download directly in the terminal, for
example from a Google Drive or Dropbox share link, into
`~/freighttg/data/_external/`. Never commit data to Git.

## Tools In The Browser

After you log in with your token, these open in a browser tab, with no VS Code
installed on your computer:

- **VS Code:** `http://192.168.1.11:30888/vscode/` from your lab computer, or
  `http://localhost:8888/vscode/` through the tunnel. Your settings and
  extensions are kept in your home folder. Open the `~/freighttg` folder to
  work on the project; its terminal runs on the lab machine, with the GPU.
- **RStudio:** `.../rstudio/`, running R 4.5 on the lab machine with your
  home folder as its working directory. Install R packages from the RStudio
  console with `install.packages()`; they go to your home folder and are kept.
  JupyterLab also has an **R 4.5** notebook kernel.
- **TensorBoard:** `.../tensorboard/`. It shows the runs under `~/logs`, so
  point your `SummaryWriter` (or `tensorboard` callback) at `~/logs/<run name>`.
- **GPU dashboard:** in JupyterLab, the **GPU Dashboards** button in the left
  sidebar opens live GPU use and memory charts.
- **CPU and memory:** shown in JupyterLab's bottom status bar.
- **Git:** the Git panel in JupyterLab's left sidebar commits, pulls, and pushes
  with the credential you set up above.

If one is missing, ask an administrator.

## 3. Connect VS Code

1. Open a notebook in VS Code.
2. Click **Select Kernel** > **Select Another Kernel** > **Existing Jupyter
   Server**.
3. Enter `http://localhost:8888/?token=<your token>`, give the server a name
   such as `glasslab`, and choose the **Python (freighttg)** kernel.

As with Colab, the kernel runs on the lab machine, so file paths in your cells
refer to the notebook's disk, not your computer. Start notebooks with:

```python
%cd ~/freighttg
import torch
print(torch.cuda.get_device_name(0))   # NVIDIA RTX A4000
```

## Moving From Colab

Cells that use `google.colab` (Colab secrets, `drive.mount`) fail here. Guard
them so the same notebook runs in both places:

```python
try:
    import google.colab  # noqa: F401
    IN_COLAB = True
except ImportError:
    IN_COLAB = False
```

On the lab notebook the repository is already cloned and your Git credential is
already configured, so skip the token and clone cells when `IN_COLAB` is
`False`, and run `git pull`, `git commit`, and `git push` from the JupyterLab
terminal or with `!git ...` in a cell.

## Good To Know

- **The GPU is shared.** Lab research jobs can take priority. If they do, your
  notebook restarts and waits until the GPU is free; your files are safe, but
  running cells stop. Save checkpoints to your home folder during long training.
- **Idle kernels are stopped after two hours** to release GPU memory. A kernel
  that is still running a cell is never stopped.
- **Long training runs** are safer as a script in a terminal with `nohup` than
  in a notebook cell, because they keep running if VS Code or the tunnel
  disconnects:

  ```bash
  cd ~/freighttg
  nohup ~/venvs/freighttg/bin/python -m freighttg.modeling.train > train.log 2>&1 &
  ```

- **Internet access is HTTPS only.** `pip install`, GitHub over HTTPS, and
  Hugging Face work. SSH to GitHub (port 22) and lab machines do not.
- **Never share your token.** If it leaks, ask an administrator to rotate it.
