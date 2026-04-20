"""Synthetic data generator backed by the external GPU service.

Two modes:
- training: Generate labeled CSV (text, is_suicide, is_toxicity) and upload to S3.
- test: Generate one message at a time and POST to the API endpoint.

Per D-09: source = "synthetic" — normalized provenance.
Per D-10: Target ~10K synthetic rows (training mode).
Per D-11: Oversamples minority classes (toxic/suicide).
Per D-13: Labels assigned from the GPU service response schema.
Per D-15: Upload to proj09_Data/zulip-raw-messages/synthetic/.
"""

import argparse
import csv
import io
import json
import logging
import random
import time
from typing import Optional

import requests

from src.data.prompts import LabelType
from src.gpu_service.models import TestResponse, TrainingResponse, TrainingRow
from src.utils.config import config
from src.utils.minio_client import get_minio_client

logger = logging.getLogger(__name__)

TARGET_TOTAL = 10_000
DEFAULT_API_URL = "http://localhost:8000/messages"


def call_gpu_service(mode: str, count: int, label: Optional[str] = None) -> dict:
    """POST to the external GPU service and return the parsed JSON response."""

    url = f"{config.GPU_SERVICE_URL}/generate"
    headers: dict[str, str] = {}
    if config.GPU_SERVICE_API_KEY:
        headers["X-API-Key"] = config.GPU_SERVICE_API_KEY
    payload: dict[str, str | int] = {"mode": mode, "count": count}
    if label is not None:
        payload["label"] = label
    response = requests.post(
        url,
        json=payload,
        headers=headers,
        timeout=config.GPU_SERVICE_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def generate_training_data(
    target_total: int = TARGET_TOTAL,
    bucket: str = config.BUCKET_RAW,
) -> dict[LabelType, int]:
    """Generate labeled synthetic data via the GPU service and upload to S3."""

    logger.info("Requesting %d training rows from GPU service...", target_total)
    response = call_gpu_service(mode="training", count=target_total)
    validated = TrainingResponse.model_validate(response)
    if not validated.rows:
        raise ValueError(f"GPU service returned no rows: {response!r}")

    rows: list[TrainingRow] = []
    for row in validated.rows:
        if not row.text.strip():
            raise ValueError(f"GPU service returned empty text row: {row!r}")
        rows.append(row)

    minio = get_minio_client()
    if not minio.bucket_exists(bucket):
        minio.make_bucket(bucket)

    output = io.StringIO()
    fieldnames = ["text", "is_suicide", "is_toxicity"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(row.model_dump() for row in rows)

    csv_bytes = output.getvalue().encode("utf-8")
    object_name = "zulip-raw-messages/synthetic/synthetic_data.csv"
    minio.put_object(
        bucket_name=bucket,
        object_name=object_name,
        data=io.BytesIO(csv_bytes),
        length=len(csv_bytes),
        content_type="text/csv",
    )
    logger.info("Uploaded %d rows to %s/%s", len(rows), bucket, object_name)

    counts: dict[LabelType, int] = {"toxic": 0, "suicide": 0, "benign": 0}
    for row in rows:
        if row.is_suicide == 1 and row.is_toxicity == 0:
            counts["suicide"] += 1
        elif row.is_toxicity == 1:
            counts["toxic"] += 1
        else:
            counts["benign"] += 1
    return counts


def generate_test_message(
    label_type: Optional[LabelType] = None,
    api_url: str = DEFAULT_API_URL,
) -> dict:
    """Generate a single test message via the GPU service and POST it to the API."""

    if label_type is None:
        label_type = random.choice(["toxic", "suicide", "benign"])

    logger.info("Requesting test message from GPU service [%s]...", label_type)
    response = call_gpu_service(mode="test", count=1, label=label_type)
    validated = TestResponse.model_validate(response)
    if not validated.texts:
        raise ValueError(f"GPU service returned no texts: {response!r}")

    text = validated.texts[0].strip()
    if not text:
        raise ValueError(f"GPU service returned empty text: {response!r}")

    logger.info("Generated [%s]: %s", label_type, text[:80])
    api_response = requests.post(
        api_url,
        json={
            "text": text,
            "user_id": f"synth-test-{label_type}",
            "source": "synthetic",
        },
        timeout=10,
    )
    api_response.raise_for_status()
    response_json = api_response.json()
    logger.info("API response: %s", response_json)

    return {
        "label_type": label_type,
        "text": text,
        "api_response": response_json,
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Generate synthetic data using the external GPU service"
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
        for index in range(args.count):
            label = args.label or random.choice(["toxic", "suicide", "benign"])
            result = generate_test_message(label_type=label, api_url=args.api_url)
            results.append(result)
            logger.info(
                "[%d/%d] %s: %s",
                index + 1,
                args.count,
                label,
                result["text"][:60],
            )
            if index < args.count - 1:
                time.sleep(args.interval)

        print(json.dumps(results, indent=2))
