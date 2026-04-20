import logging
import os
import time

import psycopg2
import torch
import torch.quantization
import yaml
from fastapi import FastAPI
from pydantic import BaseModel
from transformers import AutoModelForSequenceClassification, AutoTokenizer

log = logging.getLogger(__name__)

# Load configuration from YAML
with open("serving_config.yaml", "r") as f:
    config = yaml.safe_load(f)

app = FastAPI()

# Setup Model & Tokenizer
MODEL_NAME = config["model_name"]
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
model = AutoModelForSequenceClassification.from_pretrained(MODEL_NAME)

# Move to GPU/MPS if available — quantize_dynamic is CPU-only so only apply on CPU.
if torch.backends.mps.is_available():
    device = torch.device("mps")
elif torch.cuda.is_available():
    device = torch.device("cuda")
else:
    device = torch.device("cpu")
    # INT8 quantization: CPU-only optimization, applied before moving to device
    model = torch.quantization.quantize_dynamic(model, {torch.nn.Linear}, dtype=torch.qint8)

model.eval()
model.to(device)


# Data structure matching Input JSON
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


# Prediction Logic
def get_prediction(text: str):
    inputs = tokenizer(text, return_tensors="pt", truncation=True, padding=True).to(device)
    with torch.no_grad():
        outputs = model(**inputs)
        probs = torch.nn.functional.softmax(outputs.logits, dim=-1)
        toxicity_score = probs[0][1].item()

    # NOTE: hateBERT doesn't have a native self-harm head so we mock it
    # for now. In full production, this would hit a second model head.
    self_harm_score = toxicity_score * 0.5
    return toxicity_score, self_harm_score


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


@app.get("/health")
def health():
    return {"status": "ok"}


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

    # Use the pre-cleaned text provided by the data pipeline
    text_to_score = request.message.cleaned_text

    # Run Inference
    toxicity, self_harm = get_prediction(text_to_score)

    # Logic Tiers
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

    # Response (Output JSON)
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
