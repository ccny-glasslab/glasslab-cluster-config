FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

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
