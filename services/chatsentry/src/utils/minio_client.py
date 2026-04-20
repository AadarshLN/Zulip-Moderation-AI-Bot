import logging

from minio import Minio

from .config import config

logger = logging.getLogger(__name__)


def get_minio_client() -> Minio:
    client = Minio(
        endpoint=config.S3_ENDPOINT,
        access_key=config.MINIO_ACCESS_KEY,
        secret_key=config.MINIO_SECRET_KEY,
        secure=config.S3_SECURE,
        region="",
    )
    logger.info("S3 client connected to %s", config.S3_ENDPOINT)
    return client
