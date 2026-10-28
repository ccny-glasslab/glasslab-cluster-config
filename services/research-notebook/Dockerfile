# Personal GPU research notebook (kubeadm/glasslab-v2/research-notebooks).
#
# The official Jupyter Docker Stacks PyTorch CUDA 12 image, plus browser tools
# behind Jupyter's token login: VS Code (code-server) at /vscode/, TensorBoard
# at /tensorboard/, the NVIDIA GPU dashboard, a CPU/memory meter, and the Git
# panel (which the base image already ships). Everything is pinned.
FROM quay.io/jupyter/pytorch-notebook:cuda12-2026-09-29@sha256:66870cbf4387a3bc605e022f0698f74f79ab6cd585bcc62ec866728ef6b66c7a

ARG GLASSLAB_GIT_SHA=unknown
ARG GLASSLAB_BUILD_SOURCE=local

# code-server's release tarball, checked against the sha256 GitHub publishes as
# the asset digest for this release (also verified against a download).
ARG CODE_SERVER_VERSION=4.140.0
ARG CODE_SERVER_SHA256=864c5d01c808ade57e4d12c708717be7a187219fded60428f263b9e2da9f6b48

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

USER root
RUN curl -fsSL --retry 3 -o /tmp/code-server.tar.gz \
        "https://github.com/coder/code-server/releases/download/v${CODE_SERVER_VERSION}/code-server-${CODE_SERVER_VERSION}-linux-amd64.tar.gz" \
    && echo "${CODE_SERVER_SHA256}  /tmp/code-server.tar.gz" | sha256sum --check - \
    && mkdir /opt/code-server \
    && tar -xzf /tmp/code-server.tar.gz -C /opt/code-server --strip-components=1 --no-same-owner \
    && rm /tmp/code-server.tar.gz \
    && ln -s /opt/code-server/bin/code-server /usr/local/bin/code-server \
    && HOME=/tmp/code-server-check code-server --version \
    && rm -rf /tmp/code-server-check

USER ${NB_UID}

# Resolve against the image's own package versions as constraints, so these
# additions never upgrade JupyterLab, Jupyter Server, or PyTorch. The image's
# jupyterlab-git is kept as shipped.
RUN pip list --format=freeze > /tmp/base-constraints.txt \
    && pip install --no-cache-dir --constraint /tmp/base-constraints.txt \
        jupyter-server-proxy==4.6.0 \
        jupyter-vscode-proxy==0.7 \
        jupyter-tensorboard-proxy==0.1.1 \
        tensorboard==2.21.0 \
        jupyterlab-nvdashboard==0.15.0 \
        jupyter-resource-usage==1.3.0 \
    && rm /tmp/base-constraints.txt \
    && fix-permissions "${CONDA_DIR}" \
    && fix-permissions "/home/${NB_USER}"

# Fail the build if any tool is missing rather than shipping a notebook
# without it.
RUN jupyter server extension list 2>&1 | tee /tmp/server-extensions.txt \
    && for extension in jupyter_server_proxy jupyterlab_git jupyter_resource_usage jupyterlab_nvdashboard; do \
         grep -q "${extension} .*OK" /tmp/server-extensions.txt || { echo "missing server extension ${extension}" >&2; exit 1; }; \
       done \
    && jupyter labextension list 2>&1 | tee /tmp/lab-extensions.txt \
    && for extension in @jupyterhub/jupyter-server-proxy @jupyterlab/git @jupyter-server/resource-usage jupyterlab-nvdashboard; do \
         grep -q "${extension} .*enabled.*OK" /tmp/lab-extensions.txt || { echo "missing lab extension ${extension}" >&2; exit 1; }; \
       done \
    && python -c "import jupyter_vscode_proxy, jupyter_tensorboard_proxy" \
    && command -v tensorboard \
    && rm /tmp/server-extensions.txt /tmp/lab-extensions.txt

# code-server runs with its own auth turned off, so it listens on a mode-0600
# Unix socket that only Jupyter's token-checked /vscode/ proxy reaches. Its
# settings and extensions live in the home volume, so they persist.
ENV JUPYTER_VSCODE_PROXY_USE_SOCKET=true \
    CODE_WORKINGDIR=/home/${NB_USER} \
    CODE_EXTENSIONSDIR=/home/${NB_USER}/.local/share/code-server/extensions

LABEL org.opencontainers.image.source="https://github.com/ccny-glasslab/glasslab-cluster-config" \
      org.opencontainers.image.revision="${GLASSLAB_GIT_SHA}" \
      org.glasslab.build-source="${GLASSLAB_BUILD_SOURCE}"

WORKDIR /home/${NB_USER}
