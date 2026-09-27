FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

ARG OPENCODE_VERSION=1.18.14
ARG HERMES_VERSION=v2026.8.3
# Issue #603: the Hermes bootstrap below is fetched and executed as root. Pin
# its exact bytes (curl the URL, review the diff, then update this hash) so a
# changed upstream script fails the build at sha256sum -c instead of silently
# executing new code. A hash mismatch is intentional; bump it deliberately.
ARG HERMES_INSTALL_SHA256=abca7d8aed691fe608d794bd44e33ffc6f68513c5b4e9270d176701e16afccd9

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git nodejs npm ca-certificates curl xz-utils libatomic1 \
    && npm install --global "opencode-ai@${OPENCODE_VERSION}" \
    && curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o /tmp/install-hermes.sh \
    && echo "${HERMES_INSTALL_SHA256}  /tmp/install-hermes.sh" | sha256sum -c - \
    # The orchestrator does not use Hermes' browser tooling; the pinned
    # bootstrap supports --skip-browser. It no longer carries the npm-workspace
    # block the old sed patched, so keep the patch fail-closed (issue #603): if
    # a future pinned revision reintroduces the block, patch it and assert the
    # patch applied; otherwise assert the script still supports the
    # --skip-browser contract this image depends on. A silent no-op sed is the
    # failure mode being fixed.
    && if grep -qF 'if [ -f "$INSTALL_DIR/package.json" ]; then' /tmp/install-hermes.sh; then \
         sed -i 's|if \[ -f "\$INSTALL_DIR/package.json" \]; then|if false; then|' /tmp/install-hermes.sh \
         && ! grep -qF 'if [ -f "$INSTALL_DIR/package.json" ]; then' /tmp/install-hermes.sh; \
       else \
         grep -qF -- '--skip-browser' /tmp/install-hermes.sh; \
       fi \
    && bash /tmp/install-hermes.sh \
        --branch "${HERMES_VERSION}" --skip-setup --skip-browser \
    && rm -f /tmp/install-hermes.sh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY services/research-orchestrator/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Dense-retrieval model runtime (corpus-RAG method advisory). The import-time
# dependencies of that surface (numpy, PyMuPDF) ship via requirements.txt;
# torch/sentence-transformers are imported lazily so missing model weights at
# runtime degrade to lexical retrieval instead of breaking startup.
COPY services/research-orchestrator/requirements-dense.txt ./
RUN pip install --no-cache-dir -r requirements-dense.txt

# Shared task-fabric protocol package, installed from the checked-out source.
COPY services/task-fabric /app/services/task-fabric
RUN pip install --no-cache-dir /app/services/task-fabric

ARG GLASSLAB_GIT_SHA=unknown
ARG GLASSLAB_BUILD_SOURCE=unspecified
LABEL org.opencontainers.image.revision="${GLASSLAB_GIT_SHA}"
LABEL org.opencontainers.image.source="https://github.com/ccny-glasslab/glasslab-cluster-config"
LABEL io.glasslab.build-source="${GLASSLAB_BUILD_SOURCE}"

COPY services/research-orchestrator/app ./app
COPY services/research-orchestrator/prompts ./prompts
COPY services/research-orchestrator/evaluation-contracts ./evaluation-contracts
# Versioned routing evidence/table read at runtime by app/model_routing.py (#433).
COPY services/research-orchestrator/fixtures ./fixtures
# Operational migration tooling is part of the reviewed image so a live store
# cutover does not depend on copying executable files into a running pod.
COPY services/research-orchestrator/scripts ./scripts

RUN useradd --uid 10001 --create-home orchestrator \
    && mkdir -p /var/lib/glasslab-research-orchestrator \
    && chown -R orchestrator:orchestrator /var/lib/glasslab-research-orchestrator

USER 10001:10001
EXPOSE 8080 4210 4211 4310 4311

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
