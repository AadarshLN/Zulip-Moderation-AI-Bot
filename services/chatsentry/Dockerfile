# ChatSentry API — data ingestion and moderation pipeline
#
# Build and push:
#   docker build -t kichanitish/chatsentry-api:latest .
#   docker push kichanitish/chatsentry-api:latest
#
# Image is referenced in infra/k8s/platform/chatsentry.yaml

FROM python:3.12-slim

WORKDIR /app

# Install only API dependencies — torch/transformers are for offline synthetic
# generation only and would bloat the image by ~3GB unnecessarily.
COPY requirements.txt .
RUN pip install --no-cache-dir \
    fastapi>=0.135.0 \
    "starlette>=0.41.0,<1.0.0" \
    uvicorn>=0.42.0 \
    minio>=7.2.0 \
    psycopg2-binary>=2.9.0 \
    pandas>=3.0.0 \
    python-dotenv>=1.2.0 \
    pyyaml>=6.0.0 \
    ftfy>=6.3.1 \
    emoji>=2.15.0 \
    markdownify>=1.2.2 \
    jinja2>=3.0.0

COPY src/ ./src/
COPY config/ ./config/

EXPOSE 8000

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
