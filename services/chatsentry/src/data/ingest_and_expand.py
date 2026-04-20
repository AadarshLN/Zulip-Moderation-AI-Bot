"""CSV ingestion script — reads combined_dataset.csv in chunks and uploads to MinIO.

Per D-05: Uploads CSV as-is (no Parquet conversion).
Per D-06: 50,000 rows per chunk.
Per D-07: MinIO only — does NOT load into PostgreSQL.
Per D-08: Folder structure: zulip-raw-messages/real/combined_dataset/chunk_NNN.csv
"""

import io
import logging
import os
import sys

import pandas as pd

from src.utils.config import config
from src.utils.minio_client import get_minio_client

logger = logging.getLogger(__name__)

CHUNK_SIZE = config.CHUNK_SIZE  # From pipeline.yaml (D-04, D-07)
CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "combined_dataset.csv")


def ingest_csv(csv_path: str = CSV_PATH, bucket: str = config.BUCKET_RAW) -> int:
    """Read CSV in chunks and upload to MinIO.

    Args:
        csv_path: Path to the combined_dataset.csv file.
        bucket: MinIO bucket name (default: zulip-raw-messages).

    Returns:
        Number of chunks uploaded.

    Raises:
        FileNotFoundError: If csv_path does not exist.
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    client = get_minio_client()

    # Ensure bucket exists (fallback if minio-init hasn't run)
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
        logger.info("Created bucket: %s", bucket)

    chunk_iter = pd.read_csv(
        csv_path,
        chunksize=CHUNK_SIZE,
        encoding="utf-8",
        on_bad_lines="warn",  # Pitfall 3: handle encoding errors gracefully
    )

    chunk_count = 0
    for i, chunk in enumerate(chunk_iter):
        csv_bytes = chunk.to_csv(index=False).encode("utf-8")
        object_name = f"zulip-raw-messages/real/combined_dataset/chunk_{i:03d}.csv"

        client.put_object(
            bucket_name=bucket,
            object_name=object_name,
            data=io.BytesIO(csv_bytes),
            length=len(csv_bytes),
            content_type="text/csv",
        )

        chunk_count += 1
        logger.info("Uploaded chunk %d (%d rows) to %s/%s", i, len(chunk), bucket, object_name)

        # Free memory (Pitfall 5)
        del csv_bytes

    logger.info("Ingestion complete: %d chunks uploaded to %s", chunk_count, bucket)
    return chunk_count


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    csv_file = sys.argv[1] if len(sys.argv) > 1 else CSV_PATH
    count = ingest_csv(csv_file)
    logging.getLogger(__name__).info("Done: %d chunks uploaded", count)
