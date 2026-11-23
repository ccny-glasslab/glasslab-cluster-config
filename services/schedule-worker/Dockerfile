FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY services/schedule-worker/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/schedule-worker/app /app/app
RUN useradd --uid 10001 --no-create-home --shell /sbin/nologin glasslab
USER 10001:10001

EXPOSE 8094

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8094"]
