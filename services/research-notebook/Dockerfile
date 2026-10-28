# Personal GPU research notebook (kubeadm/glasslab-v2/research-notebooks).
#
# The official Jupyter Docker Stacks PyTorch CUDA 12 image, plus browser tools
# behind Jupyter's token login: VS Code (code-server) at /vscode/, TensorBoard
# at /tensorboard/, RStudio at /rstudio/ with an R kernel for JupyterLab, the
# NVIDIA GPU dashboard, a CPU/memory meter, and the Git panel (which the base
# image already ships). Everything is pinned.
FROM quay.io/jupyter/pytorch-notebook:cuda12-2026-09-29@sha256:66870cbf4387a3bc605e022f0698f74f79ab6cd585bcc62ec866728ef6b66c7a

ARG GLASSLAB_GIT_SHA=unknown
ARG GLASSLAB_BUILD_SOURCE=local

# code-server's release tarball, checked against the sha256 GitHub publishes as
# the asset digest for this release (also verified against a download).
ARG CODE_SERVER_VERSION=4.140.0
ARG CODE_SERVER_SHA256=864c5d01c808ade57e4d12c708717be7a187219fded60428f263b9e2da9f6b48

# RStudio Server's .deb and the sha256 Posit publishes for it in
# https://cdn.posit.co/downloads.json, which lists this jammy build for Ubuntu
# 26.04, the base image's release (also verified against a download).
ARG RSTUDIO_VERSION=2026.10.0-233
ARG RSTUDIO_SHA256=54ff3b4187a1f2b82f004b16e54caea3b81e8a397025111323c6a3d5869bf80e

# R lives in its own conda environment so installing it can never move the
# base environment's Python packages.
ARG R_ENV=/opt/conda/envs/r

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Record the base environment's Python packages; the last build step fails if
# any of them changed.
RUN pip list --format=freeze | sort > /tmp/base-python-packages.txt

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

# rserver and rsession only; jupyter-rsession-proxy starts them as the
# notebook user on demand, so no service runs at boot. rserver.conf points
# rsession at the separate R environment.
RUN curl -fsSL --retry 3 -o /tmp/rstudio-server.deb \
        "https://download2.rstudio.org/server/jammy/amd64/rstudio-server-${RSTUDIO_VERSION}-amd64.deb" \
    && echo "${RSTUDIO_SHA256}  /tmp/rstudio-server.deb" | sha256sum --check - \
    && apt-get update --yes \
    && apt-get install --yes --no-install-recommends /tmp/rstudio-server.deb \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/rstudio-server.deb \
    && printf 'rsession-which-r=%s/bin/R\nrsession-ld-library-path=%s/lib\n' "${R_ENV}" "${R_ENV}" \
        > /etc/rstudio/rserver.conf \
    && /usr/lib/rstudio-server/bin/rserver --help > /dev/null

USER ${NB_UID}

RUN mamba create --yes --prefix "${R_ENV}" r-base=4.5.3 r-irkernel=1.3.2 \
    && mamba clean --all -f -y \
    && "${R_ENV}/bin/R" --slave -e 'IRkernel::installspec(user = FALSE, sys_prefix = TRUE, displayname = "R 4.5")' \
    && printf 'R_LIBS_USER=${HOME}/R/library/%%v\n' >> "${R_ENV}/lib/R/etc/Renviron.site" \
    && printf '%s\n' \
        '# Install packages into the home volume, not the container layer.' \
        'local({ lib <- Sys.getenv("R_LIBS_USER"); dir.create(lib, recursive = TRUE, showWarnings = FALSE); .libPaths(c(lib, .libPaths())) })' \
        >> "${R_ENV}/lib/R/etc/Rprofile.site" \
    && fix-permissions "${CONDA_DIR}" \
    && fix-permissions "/home/${NB_USER}"

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
        jupyter-rsession-proxy==2.5.1 \
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
    && python -c "import jupyter_vscode_proxy, jupyter_tensorboard_proxy, jupyter_rsession_proxy" \
    && command -v tensorboard \
    && jupyter kernelspec list | grep -E '^ +ir +' \
    && HOME=/tmp/r-check "${R_ENV}/bin/R" --slave -e 'stopifnot(getRversion() == "4.5.3", startsWith(.libPaths()[1], "/tmp/r-check/R/library/4.5"))' \
    && rm -rf /tmp/r-check \
    && rm /tmp/server-extensions.txt /tmp/lab-extensions.txt \
    && pip list --format=freeze | sort > /tmp/final-python-packages.txt \
    && changed="$(comm -23 /tmp/base-python-packages.txt /tmp/final-python-packages.txt)" \
    && { [[ -z "${changed}" ]] || { echo "base Python packages changed: ${changed}" >&2; exit 1; }; } \
    && rm /tmp/base-python-packages.txt /tmp/final-python-packages.txt

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
