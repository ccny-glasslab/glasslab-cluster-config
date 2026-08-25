FROM python:3.11-slim

ARG OPENCODE_VERSION=1.18.14
ARG HERMES_VERSION=v2026.8.3

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git nodejs npm ca-certificates curl xz-utils libatomic1 \
    && npm install --global "opencode-ai@${OPENCODE_VERSION}" \
    && curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o /tmp/install-hermes.sh \
    # The orchestrator does not use Hermes' browser tooling. The upstream
    # installer still installs its npm workspace even with --skip-browser.
    && sed -i 's/if \[ -f "$INSTALL_DIR\/package.json" \]; then/if false; then/' /tmp/install-hermes.sh \
    && bash /tmp/install-hermes.sh \
        --branch "${HERMES_VERSION}" --skip-setup --skip-browser --no-skills \
    && rm -f /tmp/install-hermes.sh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY services/research-orchestrator/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

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
# Operational migration tooling is part of the reviewed image so a live store
# cutover does not depend on copying executable files into a running pod.
COPY services/research-orchestrator/scripts ./scripts

RUN useradd --uid 10001 --create-home orchestrator \
    && mkdir -p /var/lib/glasslab-research-orchestrator \
    && chown -R orchestrator:orchestrator /var/lib/glasslab-research-orchestrator

USER 10001:10001
EXPOSE 8080 4210 4211 4310 4311

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
