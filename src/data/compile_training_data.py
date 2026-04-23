"""Batch training data compiler — compiles versioned training datasets from PostgreSQL.

Two modes:
  --mode initial:     CSV chunks from MinIO, clean, bulk-load to PostgreSQL
  --mode incremental: PostgreSQL query with temporal leakage prevention

Per BATCH-01 through BATCH-05.
"""

import io
import logging
import os
import re
from datetime import datetime, timezone

import pandas as pd
from sklearn.model_selection import train_test_split

from src.data.data_quality import upload_data_docs, validate_training_data
from src.data.drift_monitor import (
    compute_batch_stats,
    load_baseline_stats,
    save_baseline_stats,
    validate_drift,
)
from src.data.text_cleaner import TextCleaner
from src.utils.config import config
from src.utils.db import get_db_connection
from src.utils.minio_client import get_minio_client

logger = logging.getLogger(__name__)

FAST_PATH_FULL_CLEAN_PATTERN = re.compile(
    r"https?://|www\.|<[^>]+>|[*_~`]|"
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b|"
    r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}|"
    r"@\w+|[\u2600-\u27BF\U0001F300-\U0001FAFF]|Ã|â|�"
)

# ---------------------------------------------------------------------------
# SQL query: incremental mode with temporal leakage prevention (BATCH-02, D-04, D-05)
# ---------------------------------------------------------------------------

INCREMENTAL_QUERY = """
    SELECT
        m.id AS message_id,
        COALESCE(m.cleaned_text, m.text) AS cleaned_text,
        m.is_suicide,
        m.is_toxicity,
        m.source,
        m.created_at,
        mod.decided_at
    FROM messages m
    INNER JOIN moderation mod ON m.id = mod.message_id
    WHERE m.created_at < mod.decided_at
    ORDER BY m.created_at
"""


def filter_temporal_leakage(df: pd.DataFrame) -> pd.DataFrame:
    """Filter rows where created_at >= decided_at (BATCH-02, D-05).

    Only messages created BEFORE the moderation decision are included.
    If decided_at column is not present (e.g. CSV data without join),
    returns df unchanged.

    Args:
        df: DataFrame potentially with 'created_at' and 'decided_at' columns.

    Returns:
        Filtered DataFrame.
    """
    if "decided_at" not in df.columns:
        return df
    return df[df["created_at"] < df["decided_at"]].copy()


def apply_quality_gate(df: pd.DataFrame) -> pd.DataFrame:
    """Filter known data quality issues before training bucket upload.

    Covers DATA_ISSUES.md:
    - Issue 4: Remove #ERROR! duplicates (262 rows)
    - Issue 5: Filter texts below 10 chars (noise)
    - Issue 5: Cap texts above 5000 chars (outliers)

    Args:
        df: DataFrame with 'cleaned_text' column.

    Returns:
        Filtered DataFrame.
    """
    cleaned = df.copy()
    initial_count = len(cleaned)

    if initial_count == 0:
        return cleaned

    cleaned["cleaned_text"] = (
        cleaned["cleaned_text"].fillna("").astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
    )
    lengths = cleaned["cleaned_text"].str.len()

    error_mask = cleaned["cleaned_text"].str.contains(
        config.QUALITY_ERROR_PATTERN,
        na=False,
    )
    short_mask = lengths < config.QUALITY_MIN_TEXT_LENGTH
    long_mask = lengths > config.QUALITY_MAX_TEXT_LENGTH

    # Remove irrecoverably bad rows, then cap remaining outliers in-place.
    cleaned = cleaned[~(error_mask | short_mask)].copy()
    cleaned["cleaned_text"] = cleaned["cleaned_text"].str[: config.QUALITY_MAX_TEXT_LENGTH]
    cleaned = cleaned.reset_index(drop=True)

    removed = initial_count - len(cleaned)
    logger.info(
        ("Quality cleanup: removed %d rows (error=%d, short=%d), truncated %d long rows"),
        removed,
        int(error_mask.sum()),
        int(short_mask.sum()),
        int(long_mask.sum()),
    )
    return cleaned


def clean_training_texts(
    text_series: pd.Series,
    cleaner: TextCleaner | None = None,
) -> pd.Series:
    """Clean raw training text with a fast path for plain-text rows.

    Most dataset rows are already plain text, so the expensive shared cleaner is
    only applied when markup, URLs, PII, emoji, or mojibake indicators are present.
    """
    if cleaner is None:
        cleaner = TextCleaner()

    cleaned = text_series.fillna("").astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
    needs_full_clean = cleaned.str.contains(FAST_PATH_FULL_CLEAN_PATTERN, na=False)

    if needs_full_clean.any():
        cleaned.loc[needs_full_clean] = cleaned.loc[needs_full_clean].apply(cleaner.clean)

    logger.info(
        "Batch cleaning fast path: %d/%d rows used full TextCleaner",
        int(needs_full_clean.sum()),
        len(cleaned),
    )
    return cleaned


def validate_and_upload_data_docs(
    df: pd.DataFrame,
    report_name: str,
) -> tuple[bool, dict]:
    """Run GE validation and upload a stage-specific HTML report."""
    success, results = validate_training_data(
        df,
        report_label=report_name.replace("-", " ").title(),
    )
    if results.get("data_docs_html"):
        upload_data_docs(
            results["data_docs_html"],
            config.BUCKET_TRAINING,
            report_name=report_name,
        )
    return success, results


def run_drift_check(df: pd.DataFrame, pre_gate_count: int, version: str) -> bool:
    """Run drift monitoring after quality gate. Returns True if within tolerance.

    Slots between apply_quality_gate() and stratified_split() in both
    compile_initial() and compile_incremental() (D-06).

    First-run bootstrap: if no baseline exists, saves current stats as baseline
    and returns True (RESEARCH.md Pitfall 1).

    Args:
        df: Post-quality-gate DataFrame with cleaned_text, is_suicide, is_toxicity.
        pre_gate_count: Row count before apply_quality_gate() for rejection rate (D-03).
        version: Version string from generate_version() for baseline metadata (D-05).
    """
    batch_stats = compute_batch_stats(df, pre_gate_count)
    baseline = load_baseline_stats(config.BUCKET_TRAINING)

    if baseline is None:
        logger.info("No baseline found — saving current batch as baseline (version %s)", version)
        save_baseline_stats(batch_stats, version, config.BUCKET_TRAINING)
        return True

    success, results = validate_drift(df, batch_stats, baseline)
    if results.get("data_docs_html"):
        upload_data_docs(
            results["data_docs_html"],
            config.BUCKET_TRAINING,
            report_name="drift-check",
        )

    if not success:
        logger.warning(
            "Drift detected in batch (version %s) — see drift-check report in ge-viewer",
            version,
        )
    return success


def bulk_load_initial_messages(df: pd.DataFrame) -> None:
    """Optionally mirror initial raw rows into PostgreSQL for later workflows."""
    load_to_postgres = os.environ.get("INITIAL_LOAD_TO_POSTGRES", "false").lower() == "true"
    if not load_to_postgres:
        logger.info("Skipping PostgreSQL bulk load in initial mode; set INITIAL_LOAD_TO_POSTGRES=true to enable it")
        return

    # Ensure a default user exists for foreign key constraint
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO users (id, username, source)
                   VALUES ('00000000-0000-0000-0000-000000000001', 'batch_pipeline', 'real')
                   ON CONFLICT (id) DO NOTHING"""
            )
            conn.commit()
    finally:
        conn.close()

    logger.info("Bulk-loading %d rows to PostgreSQL messages table", len(df))
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            for _, row in df.iterrows():
                cur.execute(
                    """INSERT INTO messages (user_id, text, cleaned_text, is_suicide, is_toxicity, source)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                    (
                        "00000000-0000-0000-0000-000000000001",
                        row["text"],
                        row["cleaned_text"],
                        bool(row["is_suicide"]),
                        bool(row.get("is_toxicity", False)),
                        row.get("source", "real"),
                    ),
                )
                msg_id = cur.fetchone()[0]
                cur.execute(
                    """INSERT INTO moderation (message_id, action, confidence, source)
                       VALUES (%s, %s, %s, %s)""",
                    (msg_id, "labeled", 1.0, row.get("source", "real")),
                )
        conn.commit()
        logger.info("Bulk load complete")
    finally:
        conn.close()


def select_output_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Select output columns for training data.

    Output columns: cleaned_text, is_suicide, is_toxicity, source, message_id.

    Args:
        df: DataFrame from either CSV or PostgreSQL source.

    Returns:
        DataFrame with exactly 5 columns.
    """
    # Ensure message_id exists (CSV rows use index or row number)
    if "message_id" not in df.columns:
        df = df.copy()
        df["message_id"] = range(len(df))

    # Ensure source exists (CSV rows default to 'real')
    if "source" not in df.columns:
        df = df.copy()
        df["source"] = "real"

    # Normalize label types: always 0/1 integers (PostgreSQL returns bools)
    df = df.copy()
    df["is_suicide"] = df["is_suicide"].astype(int)
    df["is_toxicity"] = df["is_toxicity"].astype(int)

    output_cols = ["cleaned_text", "is_suicide", "is_toxicity", "source", "message_id"]
    return df[output_cols]


def stratified_split(
    df: pd.DataFrame,
    random_state: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Stratified 70/15/15 train/val/test split (BATCH-05, D-12 through D-14).

    Uses 70/30 then 50/50 approach for clean ratios.
    Filters empty stratification classes before splitting.

    Args:
        df: DataFrame with 'is_suicide' and 'is_toxicity' columns.
        random_state: Random seed for reproducibility (default from config).

    Returns:
        Tuple of (train_df, val_df, test_df).
    """
    if random_state is None:
        random_state = config.RANDOM_STATE
    df = df.copy()

    # Create combined stratification label (4 classes, D-13)
    df["label_combo"] = df["is_suicide"].astype(str) + "_" + df["is_toxicity"].astype(str)

    # Filter empty classes (DATA_ISSUES.md Issue 2: 1_1 has 0 rows)
    label_counts = df["label_combo"].value_counts()
    populated_classes = label_counts[label_counts > 0].index
    df = df[df["label_combo"].isin(populated_classes)]

    logger.info("Stratification classes: %s", dict(label_counts[label_counts > 0]))

    # Step 1: 70% train, 30% temp (D-14)
    train_df, temp_df = train_test_split(
        df,
        test_size=1.0 - config.TRAIN_SPLIT_RATIO,  # 0.30 when ratio is 0.70
        stratify=df["label_combo"],
        random_state=random_state,
    )

    # Step 2: split 30% evenly into val (15%) and test (15%)
    val_df, test_df = train_test_split(
        temp_df,
        test_size=config.TEST_SPLIT_RATIO / (config.VAL_SPLIT_RATIO + config.TEST_SPLIT_RATIO),  # 0.50 for 15/15
        stratify=temp_df["label_combo"],
        random_state=random_state,
    )

    # Drop temporary label_combo column before returning
    train_df = train_df.drop(columns=["label_combo"])
    val_df = val_df.drop(columns=["label_combo"])
    test_df = test_df.drop(columns=["label_combo"])

    logger.info(
        "Split sizes: train=%d, val=%d, test=%d",
        len(train_df),
        len(val_df),
        len(test_df),
    )
    return train_df, val_df, test_df


def generate_version() -> str:
    """Generate UTC timestamp version string (D-09).

    Returns:
        Version string like 'v20260403-142301'.
    """
    return datetime.now(timezone.utc).strftime("v%Y%m%d-%H%M%S")


def upload_snapshot(
    client,
    bucket: str,
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
    test_df: pd.DataFrame,
    version: str | None = None,
) -> str:
    """Upload versioned training data snapshot to MinIO (BATCH-03, D-09 through D-11).

    Args:
        client: MinIO client instance.
        bucket: Target bucket name (zulip-training-data).
        train_df: Training split DataFrame.
        val_df: Validation split DataFrame.
        test_df: Test split DataFrame.

    Returns:
        Version string used for the snapshot folder.
    """
    if version is None:
        version = generate_version()

    splits = {"train": train_df, "val": val_df, "test": test_df}
    for split_name, split_df in splits.items():
        csv_bytes = split_df.to_csv(index=False).encode("utf-8")
        object_name = f"zulip-training-data/{version}/{split_name}.csv"

        client.put_object(
            bucket_name=bucket,
            object_name=object_name,
            data=io.BytesIO(csv_bytes),
            length=len(csv_bytes),
            content_type="text/csv",
        )
        logger.info(
            "Uploaded %s (%d rows) to %s/%s",
            split_name,
            len(split_df),
            bucket,
            object_name,
        )

    return version


def compile_initial() -> str | None:
    """Initial mode: read CSV from MinIO, clean, load to PostgreSQL.

    Steps:
    1. List CSV chunk objects from MinIO zulip-raw-messages/real/combined_dataset/
    2. Concatenate into single DataFrame
    3. Run TextCleaner on text column to produce cleaned_text
    4. Bulk-load to PostgreSQL messages table
    5. Select output columns
    6. GE validation + Data Docs upload
    7. Quality gate (filter #ERROR!, short/long texts)
    8. Stratified split
    9. Upload versioned snapshot to MinIO
    """
    # Read CSV chunks from S3 (real + synthetic)
    client = get_minio_client()
    cleaner = TextCleaner()

    chunks = []
    for prefix, source_tag in [
        ("zulip-raw-messages/real/combined_dataset/", "real"),
        ("zulip-raw-messages/synthetic/", "synthetic"),
    ]:
        logger.info("Reading CSV from S3 %s", prefix)
        objects = client.list_objects(
            config.BUCKET_RAW,
            prefix=prefix,
            recursive=True,
        )
        for obj in objects:
            if not obj.object_name.endswith(".csv"):
                continue
            response = client.get_object(config.BUCKET_RAW, obj.object_name)
            chunk_df = pd.read_csv(io.BytesIO(response.read()))
            if "source" not in chunk_df.columns:
                chunk_df["source"] = source_tag
            chunks.append(chunk_df)
            response.close()
            response.release_conn()

    if not chunks:
        logger.warning("No CSV chunks found in raw S3 prefixes; skipping initial snapshot")
        return None

    df = pd.concat(chunks, ignore_index=True)
    logger.info("Loaded %d rows from MinIO CSV chunks", len(df))

    # Clean text via TextCleaner
    logger.info("Running TextCleaner on text column")
    df["cleaned_text"] = clean_training_texts(df["text"], cleaner)

    bulk_load_initial_messages(df)

    # GE validation replaces SQL quality gate (D-01)
    df = select_output_columns(df)

    # GE validation (warn, generate HTML report)
    pre_clean_success, _ = validate_and_upload_data_docs(df, "before-cleaning")
    logger.info("Pre-clean GE validation success=%s", pre_clean_success)

    # Quality gate: filter data issues before training bucket (DATA_ISSUES.md Issues 4, 5)
    pre_gate_count = len(df)  # capture BEFORE gate (D-03)
    df = apply_quality_gate(df)

    post_clean_success, _ = validate_and_upload_data_docs(df, "after-cleaning")
    logger.info("Post-clean GE validation success=%s", post_clean_success)

    # Drift check (D-06): runs after gate, before split
    version = generate_version()
    run_drift_check(df, pre_gate_count, version)

    # Stratified split
    train_df, val_df, test_df = stratified_split(df)

    # Upload versioned snapshot — reuse same version string
    client = get_minio_client()
    upload_snapshot(
        client,
        config.BUCKET_TRAINING,
        train_df,
        val_df,
        test_df,
        version=version,
    )
    logger.info("Initial compilation complete: version %s", version)
    return version


def compile_incremental() -> str | None:
    """Incremental mode: query PostgreSQL, compile snapshot (BATCH-01, D-03, D-18).

    Steps:
    1. Query PostgreSQL with INNER JOIN moderation + temporal filter
    2. Apply TextCleaner fallback for NULL cleaned_text (D-18)
    3. Select output columns
    4. GE validation + Data Docs upload
    5. Quality gate (filter #ERROR!, short/long texts)
    6. Stratified split
    7. Upload versioned snapshot to MinIO
    """
    conn = get_db_connection()
    try:
        df = pd.read_sql(INCREMENTAL_QUERY, conn)
        logger.info("Incremental query returned %d rows", len(df))
    finally:
        conn.close()

    if df.empty:
        logger.warning("No new moderated messages found — skipping snapshot")
        return None

    # Temporal leakage filter (redundant with SQL WHERE but defense-in-depth)
    df = filter_temporal_leakage(df)

    # TextCleaner fallback for NULL cleaned_text (D-18)
    cleaner = TextCleaner()
    null_mask = df["cleaned_text"].isna() | (df["cleaned_text"] == "")
    if null_mask.any():
        logger.info(
            "Running TextCleaner fallback on %d rows with NULL cleaned_text",
            null_mask.sum(),
        )
        df.loc[null_mask, "cleaned_text"] = df.loc[null_mask, "text"].apply(
            lambda t: cleaner.clean(str(t)) if pd.notna(t) else "",
        )

    # Drop temporal columns before validation
    df = df.drop(columns=["created_at", "decided_at"], errors="ignore")

    # Select output columns
    df = select_output_columns(df)

    # GE validation (warn, generate HTML report)
    pre_clean_success, _ = validate_and_upload_data_docs(df, "before-cleaning")
    logger.info("Pre-clean GE validation success=%s", pre_clean_success)

    # Quality gate: filter data issues before training bucket (DATA_ISSUES.md Issues 4, 5)
    pre_gate_count = len(df)  # capture BEFORE gate (D-03)
    df = apply_quality_gate(df)

    post_clean_success, _ = validate_and_upload_data_docs(df, "after-cleaning")
    logger.info("Post-clean GE validation success=%s", post_clean_success)

    # Drift check (D-06): runs after gate, before split
    version = generate_version()
    run_drift_check(df, pre_gate_count, version)

    # Stratified split
    train_df, val_df, test_df = stratified_split(df)

    # Upload versioned snapshot
    client = get_minio_client()
    upload_snapshot(
        client,
        config.BUCKET_TRAINING,
        train_df,
        val_df,
        test_df,
        version=version,
    )
    logger.info("Incremental compilation complete: version %s", version)
    return version


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Auto-detect mode: check if PostgreSQL has data
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM messages")
            row_count = cur.fetchone()[0]
    except Exception:
        row_count = 0
    finally:
        conn.close()

    if row_count == 0:
        logger.info("PostgreSQL messages table is empty — running initial load from MinIO CSV")
        compile_initial()
    else:
        logger.info("PostgreSQL has %d messages — running incremental compilation", row_count)
        compile_incremental()
