# Zulip Moderation Bot
#
# Build and push:
#   docker buildx build --platform linux/amd64 -t kichanitish/zulip-moderation-bot:latest --push .
#
# Image is referenced in infra/k8s/platform/zulip-bot.yaml

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD ["python", "bot.py"]
