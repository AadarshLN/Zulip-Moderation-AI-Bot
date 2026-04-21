import logging
import os
import time

import psycopg2
import torch
import torch.quantization
import yaml
from fastapi import FastAPI
from minio import Minio
from pydantic import BaseModel
from transformers import AutoModel, AutoTokenizer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

with open("serving_config.yaml", "r") as f:
    config = yaml.safe_load(f)

app = FastAPI()

MODEL_NAME = config["model_name"]
S3_BUCKET = os.getenv("S3_BUCKET", "proj09_object_store")
S3_MODEL_KEY = os.getenv("S3_MODEL_KEY", "best_model.pt")


# Matches the architecture used in scripts/train.py
class TransformerMultiHeadModel(torch.nn.Module):
    def __init__(self, encoder_name: str, dropout: float = 0.1):
        super().__init__()
        self.encoder = AutoModel.from_pretrained(encoder_name)
        hidden_size = self.encoder.config.hidden_size
        self.dropout = torch.nn.Dropout(dropout)
        self.classifier = torch.nn.Linear(hidden_size, 2)

    def forward(self, input_ids, attention_mask):
        outputs = self.encoder(input_ids=input_ids, attention_mask=attention_mask)
        cls = outputs.last_hidden_state[:, 0]
        return self.classifier(self.dropout(cls))  # [batch, 2]: col0=suicide, col1=toxicity


def _ensure_model(local_path: str) -> bool:
    """Return True if model is ready at local_path — uses cache, downloads only if missing or stale."""
    endpoint = os.getenv("S3_ENDPOINT")
    access_key = os.getenv("S3_ACCESS_KEY")
    secret_key = os.getenv("S3_SECRET_KEY")
    secure = os.getenv("S3_SECURE", "true").lower() == "true"
    if not all([endpoint, access_key, secret_key]):
        log.warning("S3 credentials not set — cannot download model")
        return False
    try:
        client = Minio(endpoint=endpoint, access_key=access_key, secret_key=secret_key, secure=secure, region="")
        stat = client.stat_object(S3_BUCKET, S3_MODEL_KEY)
        s3_size = stat.size

        if os.path.exists(local_path) and os.path.getsize(local_path) == s3_size:
            print(f"[1/5] Model cache hit ({local_path}, {s3_size / 1e6:.0f} MB) — skipping download", flush=True)
            return True

        print(f"[1/5] Downloading {S3_BUCKET}/{S3_MODEL_KEY} ({s3_size / 1e6:.0f} MB)...", flush=True)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        t0 = time.time()
        client.fget_object(S3_BUCKET, S3_MODEL_KEY, local_path)
        print(f"[1/5] Download complete in {time.time() - t0:.1f}s", flush=True)
        return True
    except Exception as e:
        log.warning("Could not download model from S3: %s", e)
        return False


# ── Device selection ───────────────────────────────────────────────────────────

if torch.backends.mps.is_available():
    device = torch.device("mps")
elif torch.cuda.is_available():
    device = torch.device("cuda")
else:
    device = torch.device("cpu")

print(f"[2/5] Using device: {device}", flush=True)

# ── Model loading ──────────────────────────────────────────────────────────────

print("[3/5] Loading tokenizer...", flush=True)
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)

_ckpt_path = os.path.expanduser("~/.cache/chatsentry/best_model.pt")
if not _ensure_model(_ckpt_path):
    raise RuntimeError("Could not load model from S3 — set S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY")

print("[4/5] Loading checkpoint into TransformerMultiHeadModel...", flush=True)
model = TransformerMultiHeadModel(encoder_name=MODEL_NAME)
model.load_state_dict(torch.load(_ckpt_path, map_location=device))
MODEL_SOURCE = "trained"
MODEL_LOADED_AT = os.path.getmtime(_ckpt_path)

model.eval()

if device.type == "cpu":
    print("[5/5] Applying INT8 quantization (CPU)...", flush=True)
    model = torch.quantization.quantize_dynamic(model, {torch.nn.Linear}, dtype=torch.qint8)

model.to(device)
print(f"Model ready — source: {MODEL_SOURCE}, device: {device}", flush=True)
log.info("Model ready — source: %s, device: %s", MODEL_SOURCE, device)


# ── Request schema ─────────────────────────────────────────────────────────────


class ZulipMetadata(BaseModel):
    stream_id: int
    topic: str


class ZulipMessage(BaseModel):
    sender_email: str
    raw_text: str
    cleaned_text: str


class ZulipRequest(BaseModel):
    message_id: str
    token: str
    metadata: ZulipMetadata
    message: ZulipMessage


# ── Inference ──────────────────────────────────────────────────────────────────


def get_prediction(text: str):
    inputs = tokenizer(text, return_tensors="pt", truncation=True, padding=True)
    input_ids = inputs["input_ids"].to(device)
    attention_mask = inputs["attention_mask"].to(device)

    with torch.no_grad():
        logits = model(input_ids=input_ids, attention_mask=attention_mask)
        probs = torch.sigmoid(logits)
        self_harm_score = probs[0][0].item()
        toxicity_score = probs[0][1].item()

    return toxicity_score, self_harm_score


# ── DB logging ─────────────────────────────────────────────────────────────────


def log_to_moderation(message_id: str, action: str, confidence: float) -> None:
    """Insert a row into the moderation table. Silently skips on any DB error."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return
    try:
        conn = psycopg2.connect(database_url)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO moderation (message_id, action, confidence, model_version, source)
                    VALUES (%s::uuid, %s, %s, %s, 'real')
                    """,
                    (message_id, action, confidence, MODEL_NAME),
                )
        conn.close()
    except Exception as e:
        log.warning("moderation table write skipped: %s", e)


# ── Endpoints ──────────────────────────────────────────────────────────────────


@app.get("/health")
def health():
    return {"status": "ok", "model_source": MODEL_SOURCE}


@app.get("/model-info")
def model_info():
    return {
        "model_name": MODEL_NAME,
        "model_source": MODEL_SOURCE,
        "model_loaded_at_timestamp": MODEL_LOADED_AT,
        "cache_path": _ckpt_path,
    }


@app.get("/metrics")
def metrics():
    """Action distribution, average confidence, and 24h score drift from moderation table."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return {"error": "DATABASE_URL not set"}
    try:
        conn = psycopg2.connect(database_url)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        action,
                        COUNT(*)          AS count,
                        AVG(confidence)   AS avg_confidence
                    FROM moderation
                    WHERE model_version = %s
                    GROUP BY action
                    ORDER BY count DESC
                    """,
                    (MODEL_NAME,),
                )
                rows = cur.fetchall()

                cur.execute(
                    """
                    SELECT
                        AVG(CASE WHEN decided_at >= NOW() - INTERVAL '24 hours'
                                 THEN confidence END) AS avg_24h,
                        AVG(confidence)               AS avg_all_time,
                        COUNT(*)                      AS total
                    FROM moderation
                    WHERE model_version = %s
                    """,
                    (MODEL_NAME,),
                )
                summary = cur.fetchone()
        conn.close()

        action_breakdown = [{"action": r[0], "count": r[1], "avg_confidence": round(r[2], 4)} for r in rows]
        avg_24h, avg_all_time, total = summary
        drift = round(float(avg_24h or 0) - float(avg_all_time or 0), 4)

        return {
            "model_version": MODEL_NAME,
            "model_source": MODEL_SOURCE,
            "total_decisions": total,
            "avg_confidence_24h": round(float(avg_24h or 0), 4),
            "avg_confidence_all_time": round(float(avg_all_time or 0), 4),
            "score_drift_24h": drift,
            "action_breakdown": action_breakdown,
        }
    except Exception as e:
        log.warning("metrics query failed: %s", e)
        return {"error": str(e)}


@app.post("/moderate")
async def moderate_message(request: ZulipRequest):
    start_time = time.time()

    toxicity, self_harm = get_prediction(request.message.cleaned_text)

    response_action = "ALLOW"
    reason = "Safe content"

    if self_harm > config["thresholds"]["self_harm_alert"]:
        response_action = "ALERT_ADMIN"
        reason = "High self-harm confidence detected. Messaging mental health resources."
    elif toxicity > config["thresholds"]["toxicity_high"]:
        response_action = "HIDE_AND_STRIKE"
        reason = "High toxicity confidence. Message hidden, strike recorded."
    elif toxicity > config["thresholds"]["toxicity_medium"]:
        response_action = "WARN_AND_OBSCURE"
        reason = "Medium toxicity confidence. Warning sent to user."

    latency_ms = (time.time() - start_time) * 1000

    log_to_moderation(request.message_id, response_action, toxicity)

    return {
        "message_id": request.message_id,
        "action": response_action,
        "reason": reason,
        "scores": {"toxicity": round(toxicity, 4), "self_harm": round(self_harm, 4)},
        "thresholds_used": config["thresholds"],
        "latency_ms": round(latency_ms, 2),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
