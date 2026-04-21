"""Drift monitoring for training data pipeline.

Computes batch statistics, manages baseline in MinIO, and validates
distributions via Great Expectations. Per D-01 through D-07.
"""

import io
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import great_expectations as gx
import pandas as pd
from great_expectations.core.expectation_suite import ExpectationSuite

from src.utils.minio_client import get_minio_client

logger = logging.getLogger(__name__)

BASELINE_OBJECT = "baseline_stats.json"
MIN_VOCAB_BATCH_SIZE = 500

DEFAULT_DRIFT_THRESHOLDS = {
    "class_balance_tolerance": 0.50,
    "text_length_tolerance": 0.30,
    "rejection_rate_tolerance": 0.50,
    "vocab_overlap_min": 0.30,
}


def compute_batch_stats(df: pd.DataFrame, pre_gate_count: int) -> dict[str, Any]:
    """Compute drift-relevant statistics from post-gate DataFrame.

    Args:
        df: Post-quality-gate DataFrame (cleaned_text, is_suicide, is_toxicity).
        pre_gate_count: Row count BEFORE quality gate (for rejection rate D-03).

    Returns:
        Dict matching baseline_stats.json schema.
    """
    text_lengths = df["cleaned_text"].str.len()
    all_tokens: Counter = Counter()
    for text in df["cleaned_text"]:
        all_tokens.update(str(text).lower().split())

    rejection_rate = 1.0 - (len(df) / pre_gate_count) if pre_gate_count > 0 else 0.0

    return {
        "class_balance": {
            "is_toxicity_mean": float(df["is_toxicity"].mean()),
            "is_suicide_mean": float(df["is_suicide"].mean()),
        },
        "text_length": {
            "mean": float(text_lengths.mean()),
            "median": float(text_lengths.median()),
            "stdev": float(text_lengths.std()),
        },
        "cleaning_rejection_rate": rejection_rate,
        "vocabulary": {
            "unique_word_count": len(all_tokens),
            "top_50_tokens": dict(all_tokens.most_common(50)),
        },
    }


def load_baseline_stats(bucket: str) -> dict | None:
    """Load baseline stats from MinIO. Returns None if not found (Pitfall 1)."""
    client = get_minio_client()
    try:
        response = client.get_object(bucket, BASELINE_OBJECT)
        data = json.loads(response.read().decode("utf-8"))
        response.close()
        response.release_conn()
        return data
    except Exception:
        return None


def save_baseline_stats(stats: dict, version: str, bucket: str) -> str:
    """Save baseline stats to MinIO."""
    client = get_minio_client()
    payload = {
        "version": version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        **stats,
    }
    data = json.dumps(payload, indent=2).encode("utf-8")
    client.put_object(
        bucket_name=bucket,
        object_name=BASELINE_OBJECT,
        data=io.BytesIO(data),
        length=len(data),
        content_type="application/json",
    )
    logger.info("Saved baseline stats to %s/%s", bucket, BASELINE_OBJECT)
    return BASELINE_OBJECT


def _check_rejection_rate(batch_stats: dict, baseline_stats: dict, thresholds: dict) -> bool:
    """Check if cleaning rejection rate is within tolerance of baseline."""
    baseline_rate = baseline_stats.get("cleaning_rejection_rate", 0.0)
    current_rate = batch_stats.get("cleaning_rejection_rate", 0.0)
    tolerance = thresholds.get("rejection_rate_tolerance", 0.50)
    delta = abs(baseline_rate) * tolerance
    return abs(current_rate - baseline_rate) <= delta + 1e-9


def _check_vocabulary_drift(batch_stats: dict, baseline_stats: dict, thresholds: dict, batch_size: int) -> bool:
    """Check vocabulary drift via Jaccard similarity of top-50 token sets."""
    if batch_size < MIN_VOCAB_BATCH_SIZE:
        logger.info(
            "Skipping vocab drift -- batch size %d < %d",
            batch_size,
            MIN_VOCAB_BATCH_SIZE,
        )
        return True

    baseline_tokens = set(baseline_stats.get("vocabulary", {}).get("top_50_tokens", {}).keys())
    batch_tokens = set(batch_stats.get("vocabulary", {}).get("top_50_tokens", {}).keys())
    if not baseline_tokens or not batch_tokens:
        return True

    jaccard = len(baseline_tokens & batch_tokens) / len(baseline_tokens | batch_tokens)
    min_overlap = thresholds.get("vocab_overlap_min", 0.30)
    passed = jaccard >= min_overlap
    if not passed:
        logger.warning("Vocab drift: Jaccard=%.3f < threshold=%.3f", jaccard, min_overlap)
    return passed


_DRIFT_EXPECTATION_LABELS = {
    "expect_column_mean_to_be_between": "Distribution Mean Within Tolerance",
}


def compute_drift_bounds(baseline_value: float, tolerance_pct: float) -> tuple[float, float]:
    """Compute acceptable [min, max] range from baseline +/- tolerance%."""
    delta = abs(baseline_value) * tolerance_pct
    return (baseline_value - delta, baseline_value + delta)


def build_drift_suite(
    context,
    baseline_stats: dict,
    drift_config: dict | None = None,
    suite_name: str = "drift_monitoring",
) -> ExpectationSuite:
    """Build drift GE suite with baseline-relative bounds.

    Uses a DISTINCT suite name from 'training_data_quality' to avoid
    collision (Pitfall 5).
    """
    t = {**DEFAULT_DRIFT_THRESHOLDS, **(drift_config or {})}
    suite = context.suites.add(ExpectationSuite(name=suite_name))

    # D-01: Class balance drift -- is_toxicity
    tox_base = baseline_stats["class_balance"]["is_toxicity_mean"]
    tox_min, tox_max = compute_drift_bounds(tox_base, t["class_balance_tolerance"])
    suite.add_expectation(
        gx.expectations.ExpectColumnMeanToBeBetween(
            column="is_toxicity",
            min_value=max(0.0, tox_min),
            max_value=min(1.0, tox_max),
        )
    )

    # D-01: Class balance drift -- is_suicide
    sui_base = baseline_stats["class_balance"]["is_suicide_mean"]
    sui_min, sui_max = compute_drift_bounds(sui_base, t["class_balance_tolerance"])
    suite.add_expectation(
        gx.expectations.ExpectColumnMeanToBeBetween(
            column="is_suicide",
            min_value=max(0.0, sui_min),
            max_value=min(1.0, sui_max),
        )
    )

    # D-02: Text length mean drift
    len_base = baseline_stats["text_length"]["mean"]
    len_min, len_max = compute_drift_bounds(len_base, t["text_length_tolerance"])
    suite.add_expectation(
        gx.expectations.ExpectColumnMeanToBeBetween(
            column="text_length",
            min_value=max(0.0, len_min),
            max_value=len_max,
        )
    )

    logger.info(
        "Built drift suite '%s' with %d expectations",
        suite_name,
        len(suite.expectations),
    )
    return suite


def _generate_drift_html(
    result,
    suite: ExpectationSuite,
    batch_stats: dict,
    baseline_stats: dict,
    rejection_pass: bool,
    vocab_pass: bool,
    report_label: str = "Drift Monitoring",
) -> str:
    """Generate drift HTML report following _generate_data_docs_html pattern.

    Appends scalar drift checks (rejection rate, vocab) as extra rows
    since they are not GE expectations.
    """
    rows = []
    for exp_result in result.results:
        config_obj = exp_result.expectation_config
        status = "PASS" if exp_result.success else "FAIL"
        status_class = "pass" if exp_result.success else "fail"
        column = config_obj.kwargs.get("column", "")
        label = _DRIFT_EXPECTATION_LABELS.get(config_obj.type, config_obj.type)
        rows.append(f'<tr class="{status_class}"><td>{label}</td><td>{column}</td><td>{status}</td></tr>')

    # Scalar checks rendered as plain rows (no GE backing)
    for label, passed in [
        ("Cleaning Rejection Rate Within Tolerance", rejection_pass),
        ("Vocabulary Overlap (Top-50 Tokens)", vocab_pass),
    ]:
        sc = "pass" if passed else "fail"
        rows.append(f'<tr class="{sc}"><td>{label}</td><td>batch</td><td>{"PASS" if passed else "FAIL"}</td></tr>')

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>ChatSentry Data Quality Report — {report_label}</title>
    <style>
        body {{ font-family: sans-serif; margin: 20px; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
        th {{ background-color: #4CAF50; color: white; }}
        .pass {{ background-color: #e8f5e9; }}
        .fail {{ background-color: #ffebee; }}
        .summary {{ margin-bottom: 20px; }}
    </style>
</head>
<body>
    <h1>ChatSentry Data Quality Report</h1>
    <div class="summary">
        <p><strong>Dataset Stage:</strong> {report_label}</p>
        <p><strong>Status:</strong> {"PASSED" if result.success else "FAILED"}</p>
        <p><strong>Baseline Version:</strong> {baseline_stats.get("version", "unknown")}</p>
    </div>
    <table>
        <tr><th>Expectation</th><th>Column</th><th>Status</th></tr>
        {"".join(rows)}
    </table>
</body>
</html>"""
    return html


def validate_drift(
    df: pd.DataFrame,
    batch_stats: dict[str, Any],
    baseline_stats: dict[str, Any],
    drift_config: dict | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Run drift validation suite and generate HTML report (D-07).

    Uses a fresh ephemeral context so suite names don't collide with
    the quality context (Pitfall 5).

    Returns:
        Tuple of (success: bool, results: dict with data_docs_html).
    """
    t = {**DEFAULT_DRIFT_THRESHOLDS, **(drift_config or {})}
    rejection_pass = _check_rejection_rate(batch_stats, baseline_stats, t)
    vocab_pass = _check_vocabulary_drift(batch_stats, baseline_stats, t, len(df))

    # Augment df with computed text_length column for GE (D-02)
    df = df.copy()
    df["text_length"] = df["cleaned_text"].str.len()

    context = gx.get_context(mode="ephemeral")
    suite = build_drift_suite(context, baseline_stats, drift_config)

    data_source = context.data_sources.add_pandas("pandas")
    data_asset = data_source.add_dataframe_asset(name="drift data")
    batch_definition = data_asset.add_batch_definition_whole_dataframe("whole_df")
    batch = batch_definition.get_batch(batch_parameters={"dataframe": df})
    result = batch.validate(suite)

    ge_success = result.success
    overall_success = ge_success and rejection_pass and vocab_pass

    if not overall_success:
        logger.warning("Drift detected -- review drift report in ge-viewer")
    else:
        logger.info("Drift check passed -- all metrics within tolerance")

    data_docs_html = _generate_drift_html(
        result,
        suite,
        batch_stats,
        baseline_stats,
        rejection_pass=rejection_pass,
        vocab_pass=vocab_pass,
    )

    return overall_success, {
        "statistics": result.statistics if hasattr(result, "statistics") else {},
        "expectation_results": result.results,
        "data_docs_html": data_docs_html,
    }
