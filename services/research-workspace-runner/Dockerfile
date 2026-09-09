FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

ARG GLASSLAB_GIT_SHA=unknown
ARG GLASSLAB_BUILD_SOURCE=unspecified

LABEL org.opencontainers.image.revision="${GLASSLAB_GIT_SHA}"
LABEL io.glasslab.build-source="${GLASSLAB_BUILD_SOURCE}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY services/research-workspace-runner/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/research-workspace-runner/runner.py /app/runner.py

ENTRYPOINT ["python3", "-m", "runner"]
