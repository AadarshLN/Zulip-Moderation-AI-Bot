"""
Inference monitor — runs as a K8s CronJob every 30 minutes.

Promotion trigger:
  S3 best_model.pt is newer than the cached version → rolling restart so the
  pod picks up the new weights on next startup.

Rollback trigger:
  score_drift_24h > DRIFT_THRESHOLD (model scoring systematically higher than
  historical baseline — possible model degradation or distribution shift), OR
  >ALLOW_RATE_THRESHOLD of decisions are ALLOW (model gone too permissive) →
  restore best_model_backup.pt from S3 and rolling restart.

Both triggers call `kubectl rollout restart` which lets K8s do a graceful
rolling update — the old pod stays alive until the new one passes /health.
"""

import logging
import os
import sys

import requests
from minio import Minio

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

INFERENCE_URL = os.getenv("INFERENCE_URL", "http://inference.platform.svc.cluster.local:8000")
S3_ENDPOINT = os.getenv("S3_ENDPOINT")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY")
S3_SECURE = os.getenv("S3_SECURE", "true").lower() == "true"
S3_BUCKET = os.getenv("S3_BUCKET", "proj09_object_store")
S3_MODEL_KEY = os.getenv("S3_MODEL_KEY", "best_model.pt")
S3_BACKUP_KEY = os.getenv("S3_BACKUP_KEY", "best_model_backup.pt")

CACHE_PATH = os.getenv("MODEL_CACHE_PATH", "/root/.cache/chatsentry/best_model.pt")

# Rollback triggers — both are well-justified for a toxicity/self-harm classifier:
# - Drift > 0.10: avg score in last 24h is 10 points above all-time baseline,
#   suggesting the model is over-flagging or a data distribution shift occurred.
# - Allow rate > 0.95: model is letting almost everything through — gone too
#   permissive, possibly weights corrupted or wrong model loaded.
DRIFT_THRESHOLD = float(os.getenv("DRIFT_THRESHOLD", "0.10"))
ALLOW_RATE_THRESHOLD = float(os.getenv("ALLOW_RATE_THRESHOLD", "0.95"))

DEPLOYMENT = os.getenv("K8S_DEPLOYMENT", "deployment/inference")
NAMESPACE = os.getenv("K8S_NAMESPACE", "platform")


# ── Helpers ────────────────────────────────────────────────────────────────────


def get_minio_client() -> Minio:
    return Minio(
        endpoint=S3_ENDPOINT,
        access_key=S3_ACCESS_KEY,
        secret_key=S3_SECRET_KEY,
        secure=S3_SECURE,
        region="",
    )


def get_metrics() -> dict | None:
    try:
        resp = requests.get(f"{INFERENCE_URL}/metrics", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error("Could not reach /metrics: %s", e)
        return None


def get_model_info() -> dict | None:
    try:
        resp = requests.get(f"{INFERENCE_URL}/model-info", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error("Could not reach /model-info: %s", e)
        return None


def rolling_restart() -> None:
    import subprocess

    log.info("Triggering rolling restart of %s in namespace %s", DEPLOYMENT, NAMESPACE)
    result = subprocess.run(
        ["kubectl", "rollout", "restart", DEPLOYMENT, "-n", NAMESPACE],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        log.info("Rolling restart triggered: %s", result.stdout.strip())
    else:
        log.error("kubectl failed: %s", result.stderr.strip())


def restore_backup() -> bool:
    """Copy best_model_backup.pt → best_model.pt in S3."""
    try:
        client = get_minio_client()
        client.copy_object(
            S3_BUCKET,
            S3_MODEL_KEY,
            f"{S3_BUCKET}/{S3_BACKUP_KEY}",
        )
        log.info("Restored backup: %s → %s", S3_BACKUP_KEY, S3_MODEL_KEY)
        return True
    except Exception as e:
        log.error("Backup restore failed: %s", e)
        return False


def backup_current() -> None:
    """Copy current best_model.pt → best_model_backup.pt before promoting."""
    try:
        client = get_minio_client()
        client.copy_object(
            S3_BUCKET,
            S3_BACKUP_KEY,
            f"{S3_BUCKET}/{S3_MODEL_KEY}",
        )
        log.info("Backed up current model to %s", S3_BACKUP_KEY)
    except Exception as e:
        log.warning("Could not back up current model: %s", e)


# ── Main logic ─────────────────────────────────────────────────────────────────


def check_rollback(metrics: dict) -> bool:
    """Return True if rollback should be triggered."""
    drift = metrics.get("score_drift_24h", 0)
    if drift > DRIFT_THRESHOLD:
        log.warning("ROLLBACK: score_drift_24h=%.4f > threshold=%.4f", drift, DRIFT_THRESHOLD)
        return True

    breakdown = metrics.get("action_breakdown", [])
    total = metrics.get("total_decisions", 0)
    if total > 50:
        allow_count = next((r["count"] for r in breakdown if r["action"] == "ALLOW"), 0)
        allow_rate = allow_count / total
        if allow_rate > ALLOW_RATE_THRESHOLD:
            log.warning("ROLLBACK: allow_rate=%.2f > threshold=%.2f", allow_rate, ALLOW_RATE_THRESHOLD)
            return True

    return False


def check_promotion(model_info: dict) -> bool:
    """Return True if a newer model is in S3 than what's currently loaded."""
    try:
        client = get_minio_client()
        stat = client.stat_object(S3_BUCKET, S3_MODEL_KEY)
        s3_mtime = stat.last_modified.timestamp()
        loaded_mtime = model_info.get("model_loaded_at_timestamp", 0)
        if s3_mtime > loaded_mtime:
            log.info(
                "PROMOTE: S3 model newer (s3=%.0f, loaded=%.0f)",
                s3_mtime,
                loaded_mtime,
            )
            return True
    except Exception as e:
        log.error("Could not check S3 model timestamp: %s", e)
    return False


def main() -> None:
    log.info("=== Inference monitor starting ===")

    if not all([S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY]):
        log.error("S3 credentials not set — exiting")
        sys.exit(1)

    metrics = get_metrics()
    model_info = get_model_info()

    if metrics is None or model_info is None:
        log.error("Could not reach inference service — skipping this run")
        sys.exit(1)

    log.info(
        "Metrics: total=%s drift_24h=%.4f",
        metrics.get("total_decisions"),
        metrics.get("score_drift_24h", 0),
    )

    if check_rollback(metrics):
        log.warning("Rollback triggered — restoring backup model")
        if restore_backup():
            rolling_restart()
        else:
            log.error("Rollback aborted — no backup available")
        return

    if check_promotion(model_info):
        log.info("Promotion triggered — new model detected in S3")
        backup_current()
        rolling_restart()
        return

    log.info("No action needed — model and metrics look healthy")


if __name__ == "__main__":
    main()
