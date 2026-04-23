# ChatSentry API — data ingestion and moderation pipeline
#
# Build for the k3s app node:
#   docker build -f services/chatsentry/Dockerfile -t docker.io/library/chatsentry-api:vm-local .
#   docker save docker.io/library/chatsentry-api:vm-local -o /tmp/chatsentry-api-vm-local.tar
#   sudo k3s ctr images import /tmp/chatsentry-api-vm-local.tar
#
# Image is referenced in infra/k8s/platform/chatsentry.yaml

FROM python:3.12-slim

WORKDIR /app

# Chatsentry serves the API and also runs the data-pipeline admin paths used by
# the dashboard, batch compiler, and drift checks. Keep the heavy training stack
# out of this image, but include the runtime packages those paths actually
# import so the integrated deployment can execute them on-cluster.
COPY requirements.txt .
RUN pip install --no-cache-dir \
    fastapi>=0.135.0 \
    "starlette>=0.41.0,<1.0.0" \
    uvicorn>=0.42.0 \
    jinja2>=3.0.0 \
    minio>=7.2.0 \
    psycopg2-binary>=2.9.0 \
    pandas>=3.0.0 \
    scikit-learn>=1.6.0 \
    great-expectations>=1.15.2 \
    python-dotenv>=1.2.0 \
    pyyaml>=6.0.0 \
    requests>=2.32.0 \
    ftfy>=6.3.1 \
    emoji>=2.15.0 \
    markdownify>=1.2.2 \
    jinja2>=3.0.0 \
    "prometheus-fastapi-instrumentator>=6.0.0,<7.0.0" \
    prometheus-client>=0.20.0 \
    kubernetes>=29.0.0

COPY src/ ./src/
COPY config/ ./config/

EXPOSE 8000

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
