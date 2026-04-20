"""Synthetic data generator using local Qwen2.5-1.5B model.

No HuggingFace API token needed — runs entirely locally.

Two modes:
- training: Generate labeled CSV (text, is_suicide, is_toxicity) and upload to S3.
- test: Generate one message at a time and POST to the API endpoint.

Per D-10: Target ~10K synthetic rows (training mode).
Per D-11: Oversamples minority classes (toxic/suicide).
Per D-13: Labels assigned from prompt, not post-hoc classification.
Per D-14: source = 'synthetic_local'.
Per D-15: Upload to proj09_Data/zulip-raw-messages/synthetic/.
"""

import argparse
import csv
import io
import json
import logging
import random
import time
from pathlib import Path
from typing import Optional

import requests
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

from src.data.prompts import (
    LABEL_DISTRIBUTION,
    PROMPTS_BY_LABEL,
    LabelType,
)
from src.utils.config import config
from src.utils.minio_client import get_minio_client

logger = logging.getLogger(__name__)

MODEL_ID = "Qwen/Qwen2.5-1.5B"
TARGET_TOTAL = 10_000
DEFAULT_API_URL = "http://localhost:8000/messages"

# Loaded lazily on first use
_model = None
_tokenizer = None


def _restore_model_from_s3() -> bool:
    """Download model from S3 if local cache is empty.

    Returns:
        True if model was downloaded or already cached.
    """
    cache_dir = Path.home() / ".cache/huggingface/hub"
    model_dir = cache_dir / "models--Qwen--Qwen2.5-1.5B"

    if model_dir.exists() and any(model_dir.rglob("*.safetensors")):
        logger.info("Model already cached locally")
        return True

    logger.info("Model not in cache, downloading from S3...")
    try:
        minio = get_minio_client()
        objects = list(
            minio.list_objects(
                config.BUCKET_RAW, prefix="models/Qwen2.5-1.5B/", recursive=True
            )
        )
        if not objects:
            logger.warning("No model in S3, will download from HuggingFace")
            return False

        for obj in objects:
            rel_path = obj.object_name.replace("models/Qwen2.5-1.5B/", "")
            local_path = cache_dir / "models--Qwen--Qwen2.5-1.5B" / rel_path
            local_path.parent.mkdir(parents=True, exist_ok=True)

            if local_path.exists() and local_path.stat().st_size == obj.size:
                continue  # Already downloaded

            response = minio.get_object(config.BUCKET_RAW, obj.object_name)
            with open(local_path, "wb") as f:
                for chunk in response.stream(32 * 1024):
                    f.write(chunk)
            response.close()
            logger.info("  Downloaded: %s", rel_path)

        logger.info("Model restored from S3")
        return True
    except Exception as e:
        logger.warning("S3 download failed: %s, will use HuggingFace", e)
        return False


def _load_model() -> tuple:
    """Load Qwen2.5-1.5B model and tokenizer (cached)."""
    global _model, _tokenizer
    if _model is None:
        _restore_model_from_s3()
        logger.info("Loading %s...", MODEL_ID)
        _tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
        _model = AutoModelForCausalLM.from_pretrained(
            MODEL_ID, torch_dtype=torch.float32
        )
        logger.info(
            "Model loaded: %.0fM params",
            sum(p.numel() for p in _model.parameters()) / 1e6,
        )
    return _model, _tokenizer


def generate_text(
    prompt: str, max_new_tokens: int = 250, temperature: float = 0.8
) -> str:
    """Generate text from a prompt using the local model.

    Args:
        prompt: Input prompt.
        max_new_tokens: Maximum tokens to generate.
        temperature: Sampling temperature.

    Returns:
        Generated text (prompt stripped).
    """
    model, tokenizer = _load_model()
    inputs = tokenizer(prompt, return_tensors="pt")
    with torch.no_grad():
        output = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            do_sample=True,
            top_p=0.9,
            repetition_penalty=1.15,
            pad_token_id=tokenizer.eos_token_id,
        )
    return tokenizer.decode(
        output[0][inputs["input_ids"].shape[1] :], skip_special_tokens=True
    )


def _parse_numbered_list(text: str) -> list[str]:
    """Parse numbered list from generated text.

    Args:
        text: Raw text (e.g., "1. message one\n2. message two\n...").

    Returns:
        List of message strings.
    """
    messages = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # Remove "Example N:" prefix if model repeats examples
        if line.lower().startswith("example "):
            colon_pos = line.find(":")
            if colon_pos > 0:
                line = line[colon_pos + 1 :].strip()
        # Remove numbering prefix (e.g., "1. ", "2. ", etc.)
        if line and line[0].isdigit():
            dot_pos = line.find(".")
            if 0 < dot_pos <= 3:
                line = line[dot_pos + 1 :].strip()
        # Remove quotes if present
        if line and line[0] == '"' and line[-1] == '"':
            line = line[1:-1]
        if line and len(line) > 10:  # Skip very short fragments
            messages.append(line)
    return messages


def generate_batch(label_type: LabelType, count: int = 10) -> list[dict]:
    """Generate a batch of labeled messages for training data.

    Args:
        label_type: One of 'toxic', 'suicide', 'benign'.
        count: Number of messages to generate.

    Returns:
        List of dicts with text, is_suicide, is_toxicity keys.
    """
    prompt_obj = PROMPTS_BY_LABEL[label_type]
    raw = generate_text(prompt_obj.prompt)
    messages = _parse_numbered_list(raw)

    rows = []
    for msg in messages[:count]:
        rows.append(
            {
                "text": msg,
                "is_suicide": int(prompt_obj.is_suicide),
                "is_toxicity": int(prompt_obj.is_toxicity),
            }
        )
    return rows


def generate_training_data(
    target_total: int = TARGET_TOTAL,
    bucket: str = config.BUCKET_RAW,
) -> dict[LabelType, int]:
    """Generate labeled synthetic data and upload to S3.

    Args:
        target_total: Total number of synthetic rows.
        bucket: S3 bucket name.

    Returns:
        Dict mapping label type to count generated.
    """
    minio = get_minio_client()
    if not minio.bucket_exists(bucket):
        minio.make_bucket(bucket)

    counts: dict[LabelType, int] = {"toxic": 0, "suicide": 0, "benign": 0}
    all_rows: list[dict] = []

    for label_type, proportion in LABEL_DISTRIBUTION.items():
        target_count = int(target_total * proportion)
        generated = 0
        logger.info("Generating %d %s messages...", target_count, label_type)

        while generated < target_count:
            rows = generate_batch(label_type, count=10)
            if not rows:
                logger.warning(
                    "No messages generated for %s, skipping batch", label_type
                )
                break
            for row in rows:
                if generated >= target_count:
                    break
                all_rows.append(row)
                generated += 1

            counts[label_type] = generated
            logger.info("  %s: %d / %d", label_type, generated, target_count)

    # Upload to S3 as CSV
    output = io.StringIO()
    fieldnames = ["text", "is_suicide", "is_toxicity"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(all_rows)

    csv_bytes = output.getvalue().encode("utf-8")
    object_name = "zulip-raw-messages/synthetic/synthetic_data.csv"
    minio.put_object(
        bucket_name=bucket,
        object_name=object_name,
        data=io.BytesIO(csv_bytes),
        length=len(csv_bytes),
        content_type="text/csv",
    )
    logger.info("Uploaded %d rows to %s/%s", len(all_rows), bucket, object_name)
    return counts


def generate_test_message(
    label_type: Optional[LabelType] = None,
    api_url: str = DEFAULT_API_URL,
) -> dict:
    """Generate a single test message and POST it to the API.

    Args:
        label_type: One of 'toxic', 'suicide', 'benign', or None for random.
        api_url: FastAPI endpoint URL.

    Returns:
        Dict with generated text and API response.
    """
    import random

    if label_type is None:
        label_type = random.choice(["toxic", "suicide", "benign"])

    # Generate a single message (use count=1 in prompt)
    prompt_obj = PROMPTS_BY_LABEL[label_type]
    # Modify prompt to generate just 1 message
    single_prompt = (
        prompt_obj.prompt.replace("Generate 10 new", "Generate 1 new")
        .replace("numbered 1-10", "")
        .replace("1-", "1")
    )

    raw = generate_text(single_prompt, max_new_tokens=100, temperature=0.9)
    messages = _parse_numbered_list(raw)

    if not messages:
        # Fallback: use the raw text directly
        text = raw.strip().split("\n")[0][:200]
    else:
        text = messages[0]

    # Clean up the text
    text = text.strip()
    if text.startswith("- "):
        text = text[2:]

    logger.info("Generated [%s]: %s", label_type, text[:80])

    # POST to API
    try:
        resp = requests.post(
            api_url,
            json={"text": text, "user_id": f"synth-test-{label_type}"},
            timeout=10,
        )
        api_response = resp.json()
        logger.info("API response: %s", api_response)
    except Exception as e:
        api_response = {"error": str(e)}
        logger.error("API call failed: %s", e)

    return {
        "label_type": label_type,
        "text": text,
        "api_response": api_response,
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Generate synthetic data using local Qwen2.5-1.5B"
    )
    parser.add_argument(
        "--mode",
        choices=["training", "test"],
        required=True,
        help="training: generate labeled CSV for S3 | test: generate + send to API",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=TARGET_TOTAL,
        help="Rows (training) or requests (test), default: 10000/1",
    )
    parser.add_argument(
        "--label",
        choices=["toxic", "suicide", "benign"],
        default=None,
        help="Label type for test mode (default: random)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Seconds between requests in test mode (default: 2.0)",
    )
    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help="API endpoint for test mode",
    )
    parser.add_argument(
        "--bucket",
        default=config.BUCKET_RAW,
        help="S3 bucket for training mode",
    )
    args = parser.parse_args()

    if args.mode == "training":
        counts = generate_training_data(target_total=args.count, bucket=args.bucket)
        logger.info("Done: %s", counts)
    elif args.mode == "test":
        results = []
        for i in range(args.count):
            label = args.label or random.choice(["toxic", "suicide", "benign"])
            result = generate_test_message(label_type=label, api_url=args.api_url)
            results.append(result)
            logger.info(
                "[%d/%d] %s: %s",
                i + 1,
                args.count,
                label,
                result["text"][:60],
            )
            if i < args.count - 1:
                time.sleep(args.interval)

        print(json.dumps(results, indent=2))
