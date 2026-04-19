FROM python:3.9-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir fastapi uvicorn torch transformers pyyaml

# Copy app and config
COPY src/main.py .Oka
COPY serving_config.yaml .

# Expose port for Zulip
EXPOSE 8000

# Start serving with 4 workers for better RPS (throughput)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]