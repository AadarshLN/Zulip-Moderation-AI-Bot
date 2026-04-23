FROM python:3.10-slim

WORKDIR /app

# Install dependencies
# NOTE: For GPU inference on AMD ROCm nodes, replace the torch install with:
#   pip install torch --index-url https://download.pytorch.org/whl/rocm6.0
# The current setup runs on CPU with INT8 quantization.
RUN pip install --no-cache-dir fastapi uvicorn torch transformers pyyaml \
    prometheus-fastapi-instrumentator prometheus-client \
    psycopg2-binary minio mlflow

# Copy app and config
COPY main.py .
COPY serving_config.yaml .

# Expose port
EXPOSE 8000

# Start serving with 2 workers — each loads the model into memory (~500MB each).
# Increase workers if the app node has sufficient RAM and throughput is a bottleneck.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
