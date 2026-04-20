"""Stage Qwen model artifact from CHI@TACC object storage to local dir."""

import logging
from pathlib import Path

from src.utils.config import config
from src.utils.minio_client import get_minio_client

logger = logging.getLogger(__name__)

S3_MODEL_PREFIX = "models/Qwen2.5-1.5B/"


def stage_model_artifact() -> Path:
    """Stage the Qwen artifact into the configured local directory."""

    stage_dir = Path(config.GPU_MODEL_STAGE_DIR)
    stage_dir.mkdir(parents=True, exist_ok=True)

    minio = get_minio_client()
    objects = list(
        minio.list_objects(config.BUCKET_RAW, prefix=S3_MODEL_PREFIX, recursive=True)
    )
    if not objects:
        raise RuntimeError(
            f"No model artifacts found at {config.BUCKET_RAW}/{S3_MODEL_PREFIX}"
        )

    for obj in objects:
        rel_path = obj.object_name.replace(S3_MODEL_PREFIX, "")
        local_path = stage_dir / rel_path
        local_path.parent.mkdir(parents=True, exist_ok=True)

        if local_path.exists() and local_path.stat().st_size == obj.size:
            logger.debug("Already staged: %s", rel_path)
            continue

        logger.info("Staging: %s", rel_path)
        response = minio.get_object(config.BUCKET_RAW, obj.object_name)
        try:
            with open(local_path, "wb") as file_handle:
                for chunk in response.stream(32 * 1024):
                    file_handle.write(chunk)
        finally:
            response.close()

    logger.info("Model staged to %s", stage_dir)
    return stage_dir
