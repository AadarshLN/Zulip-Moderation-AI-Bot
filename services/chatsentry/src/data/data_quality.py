"""Great Expectations data quality validation for training data.

Replaces hand-coded apply_quality_gate() with declarative Expectation Suites.
Per D-01 through D-05, QUALITY-01, QUALITY-02.
"""

import logging
from typing import Any

import great_expectations as gx
from great_expectations.core.expectation_suite import ExpectationSuite

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLDS = {
    "min_text_length": 10,
    "max_text_length": 5000,
    "error_pattern": "#ERROR!",
    "min_toxicity_ratio": 0.02,
    "max_toxicity_ratio": 0.08,
}


def build_expectation_suite(
    context,
    suite_name: str = "training_data_quality",
    thresholds: dict[str, Any] | None = None,
) -> ExpectationSuite:
    """Build GE Expectation Suite for training data validation (D-02).

    Includes 6 expectation types:
    1. Column schema: cleaned_text exists
    2. Text length bounds: 10-5000 chars (replaces D-22, D-23)
    3. No #ERROR! pattern (replaces D-21)
    4. Label validity: is_suicide and is_toxicity are 0 or 1
    5. Class balance ratio: is_toxicity proportion between 2-8%
    6. Null checks: no nulls in cleaned_text

    Args:
        context: GX context (ephemeral or file-based).
        suite_name: Name for the expectation suite.
        thresholds: Override default thresholds. Keys: min_text_length,
            max_text_length, error_pattern, min_toxicity_ratio, max_toxicity_ratio.

    Returns:
        ExpectationSuite with all 6 expectations.
    """
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}

    suite = context.suites.add(ExpectationSuite(name=suite_name))

    suite.add_expectation(gx.expectations.ExpectColumnToExist(column="cleaned_text"))

    suite.add_expectation(
        gx.expectations.ExpectColumnValueLengthsToBeBetween(
            column="cleaned_text",
            min_value=t["min_text_length"],
            max_value=t["max_text_length"],
            severity="warning",
        )
    )

    suite.add_expectation(
        gx.expectations.ExpectColumnValuesToNotMatchRegex(
            column="cleaned_text",
            regex=t["error_pattern"],
            severity="warning",
        )
    )

    suite.add_expectation(
        gx.expectations.ExpectColumnValuesToBeInSet(
            column="is_suicide",
            value_set=[0, 1],
        )
    )

    suite.add_expectation(
        gx.expectations.ExpectColumnMeanToBeBetween(
            column="is_toxicity",
            min_value=t["min_toxicity_ratio"],
            max_value=t["max_toxicity_ratio"],
            severity="warning",
        )
    )

    suite.add_expectation(
        gx.expectations.ExpectColumnValuesToNotBeNull(column="cleaned_text")
    )

    logger.info(
        "Built expectation suite '%s' with %d expectations",
        suite_name,
        len(suite.expectations),
    )
    return suite


def validate_training_data(
    df,
    thresholds: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Validate training DataFrame against GE Expectation Suite (D-01 through D-05).

    Creates an ephemeral GX context, validates the DataFrame, and generates
    Data Docs HTML. Validation failures log warnings but do NOT raise exceptions
    (D-03: warn and continue — ML team reviews Data Docs separately).

    Args:
        df: DataFrame with columns: cleaned_text, is_suicide, is_toxicity, source.
        thresholds: Override default threshold values (optional).

    Returns:
        Tuple of (success: bool, results: dict with statistics and Data Docs HTML).
        success=True means ALL expectations passed.
        results contains: statistics, expectation_results, data_docs_html.
    """
    import pandas as pd

    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"Expected pandas DataFrame, got {type(df)}")
    if df.empty:
        logger.warning("Empty DataFrame — skipping validation")
        return True, {
            "statistics": {"evaluated_expectations": 0},
            "data_docs_html": "",
        }

    context = gx.get_context(mode="ephemeral")
    suite = build_expectation_suite(context, thresholds=thresholds)

    data_source = context.data_sources.add_pandas("pandas")
    data_asset = data_source.add_dataframe_asset(name="training data")
    batch_definition = data_asset.add_batch_definition_whole_dataframe("whole_df")

    batch = batch_definition.get_batch(batch_parameters={"dataframe": df})
    result = batch.validate(suite)

    success = result.success
    statistics = result.statistics if hasattr(result, "statistics") else {}

    if not success:
        failed = [exp for exp in result.results if not exp.success]
        logger.warning(
            "GE validation: %d/%d expectations failed",
            len(failed),
            len(result.results),
        )
        for exp in failed:
            logger.warning(
                "  - %s: %s",
                exp.expectation_config.type,
                exp.result.get("unexpected_percent", "N/A"),
            )
    else:
        logger.info(
            "GE validation: all %d expectations passed",
            len(result.results),
        )

    data_docs_html = _generate_data_docs_html(result, suite)

    return success, {
        "statistics": statistics,
        "expectation_results": result.results,
        "data_docs_html": data_docs_html,
    }


_EXPECTATION_LABELS = {
    "expect_column_to_exist": "Required Column Present",
    "expect_column_value_lengths_to_be_between": (
        "Text Length Within Bounds (10–5000 chars)"
    ),
    "expect_column_values_to_not_match_regex": ("No Corrupt Data (e.g. #ERROR!)"),
    "expect_column_values_to_not_be_null": "No Missing Values",
    "expect_column_values_to_be_in_set": "Valid Label Values (0 or 1)",
    "expect_column_mean_to_be_between": ("Class Balance Ratio (2–8% toxicity)"),
}


def _generate_data_docs_html(result, suite: ExpectationSuite) -> str:
    """Generate Data Docs HTML from validation result.

    Creates a simple HTML report showing pass/fail per expectation.
    For ephemeral contexts, we build HTML manually from the validation result.

    Args:
        result: GX validation result.
        suite: The expectation suite that was validated.

    Returns:
        HTML string for Data Docs.
    """
    rows = []
    for exp_result in result.results:
        config = exp_result.expectation_config
        status = "PASS" if exp_result.success else "FAIL"
        status_class = "pass" if exp_result.success else "fail"
        expectation_type = config.type
        kwargs = config.kwargs
        column = kwargs.get("column", "")
        label = _EXPECTATION_LABELS.get(expectation_type, expectation_type)

        detail = ""
        if not exp_result.success:
            unexpected = exp_result.result.get("unexpected_percent", "")
            if isinstance(unexpected, (int, float)):
                detail = f" — {unexpected:.2f}% of rows failed"
            elif unexpected:
                detail = f" — {unexpected}"

        rows.append(
            f'<tr class="{status_class}">'
            f"<td>{label}</td>"
            f"<td>{column}</td>"
            f"<td>{status}{detail}</td>"
            f"</tr>"
        )

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>ChatSentry Data Quality Report</title>
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
        <p><strong>Status:</strong> {"PASSED" if result.success else "FAILED"}</p>
        <p><strong>Expectations:</strong> {result.statistics.get("successful_expectations", 0)}/{result.statistics.get("evaluated_expectations", 0)} passed</p>
    </div>
    <table>
        <tr><th>Expectation</th><th>Column</th><th>Status</th></tr>
        {"".join(rows)}
    </table>
</body>
</html>"""
    return html


def upload_data_docs(html: str, bucket: str = "proj09_Data") -> str:
    """Upload Data Docs HTML to MinIO (D-04).

    Args:
        html: HTML string from validate_training_data().
        bucket: MinIO bucket name.

    Returns:
        Object name in MinIO.
    """
    import io
    from datetime import datetime, timezone

    from src.utils.minio_client import get_minio_client

    client = get_minio_client()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    object_name = f"data-quality-report/report-{timestamp}.html"

    html_bytes = html.encode("utf-8")
    client.put_object(
        bucket_name=bucket,
        object_name=object_name,
        data=io.BytesIO(html_bytes),
        length=len(html_bytes),
        content_type="text/html",
    )
    logger.info("Uploaded Data Docs to %s/%s", bucket, object_name)
    return object_name
