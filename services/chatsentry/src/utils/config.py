import os
from dataclasses import dataclass

import yaml
from dotenv import load_dotenv

load_dotenv()

DEFAULTS_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config", "pipeline.yaml")


def _load_yaml_defaults(path: str = DEFAULTS_PATH) -> dict:
    """Load pipeline.yaml defaults. Returns empty dict if file missing."""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


# Mapping: YAML (section, key) → Config field name
_YAML_TO_CONFIG = {
    ("ingestion", "chunk_size"): "CHUNK_SIZE",
    ("ingestion", "synthetic_target_rows"): "SYNTHETIC_TARGET_ROWS",
    ("quality", "min_text_length"): "QUALITY_MIN_TEXT_LENGTH",
    ("quality", "max_text_length"): "QUALITY_MAX_TEXT_LENGTH",
    ("quality", "error_pattern"): "QUALITY_ERROR_PATTERN",
    ("split", "train_ratio"): "TRAIN_SPLIT_RATIO",
    ("split", "val_ratio"): "VAL_SPLIT_RATIO",
    ("split", "test_ratio"): "TEST_SPLIT_RATIO",
    ("split", "random_state"): "RANDOM_STATE",
    ("buckets", "raw"): "BUCKET_RAW",
    ("buckets", "training"): "BUCKET_TRAINING",
    ("traffic", "rps_target"): "RPS_TARGET",
    ("batch", "upload_size"): "MINIO_BATCH_UPLOAD_SIZE",
}


def _yaml_to_kwargs(data: dict) -> dict:
    """Convert nested YAML dict to flat Config(**kwargs).

    Uses _YAML_TO_CONFIG mapping to translate YAML section.key
    to Config field names (e.g. ingestion.chunk_size → CHUNK_SIZE).
    """
    result = {}
    for section, section_data in data.items():
        if not isinstance(section_data, dict):
            continue
        for key, value in section_data.items():
            config_field = _YAML_TO_CONFIG.get((section, key))
            if config_field is not None:
                result[config_field] = value
    return result


def _env_kwargs() -> dict:
    """Read infrastructure env vars at call time (not class definition time).

    This allows monkeypatch.setenv() to work in tests because env vars
    are evaluated when Config is instantiated, not when the class is defined.
    """
    return {
        "DATABASE_URL": os.environ.get(
            "DATABASE_URL",
            "postgresql://user:chatsentry_pg@localhost:5432/chatsentry",
        ),
        "MINIO_ENDPOINT": os.environ.get("MINIO_ENDPOINT", "localhost:9000"),
        "MINIO_ACCESS_KEY": os.environ.get("MINIO_ACCESS_KEY", "admin"),
        "MINIO_SECRET_KEY": os.environ.get("MINIO_SECRET_KEY", "chatsentry_minio"),
        "MINIO_SECURE": os.environ.get("MINIO_SECURE", "false").lower() == "true",
        "S3_ENDPOINT": os.environ.get("S3_ENDPOINT", "chi.tacc.chameleoncloud.org:7480"),
        "S3_SECURE": os.environ.get("S3_SECURE", "true").lower() == "true",
    }


@dataclass(frozen=True)
class Config:
    # --- Infrastructure (env var overrides, secrets) ---
    DATABASE_URL: str = "postgresql://user:chatsentry_pg@localhost:5432/chatsentry"
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = "admin"
    MINIO_SECRET_KEY: str = "chatsentry_minio"
    MINIO_SECURE: bool = False
    S3_ENDPOINT: str = "chi.tacc.chameleoncloud.org:7480"
    S3_SECURE: bool = True

    # --- Pipeline tunables (from YAML, per D-07) ---
    BUCKET_RAW: str = "proj09_Data"
    BUCKET_TRAINING: str = "proj09_Data"
    CHUNK_SIZE: int = 50_000
    QUALITY_MIN_TEXT_LENGTH: int = 10
    QUALITY_MAX_TEXT_LENGTH: int = 5_000
    QUALITY_ERROR_PATTERN: str = "#ERROR!"
    TRAIN_SPLIT_RATIO: float = 0.70
    VAL_SPLIT_RATIO: float = 0.15
    TEST_SPLIT_RATIO: float = 0.15
    RPS_TARGET: int = 15
    MINIO_BATCH_UPLOAD_SIZE: int = 10_000
    SYNTHETIC_TARGET_ROWS: int = 10_000
    RANDOM_STATE: int = 42


# Module-level singleton — matches existing pattern `from src.utils.config import config`
# Priority: env vars (secrets) > YAML > dataclass defaults
_yaml = _load_yaml_defaults()
_kwargs = {**_yaml_to_kwargs(_yaml), **_env_kwargs()}
config = Config(**_kwargs)
