# docker/ge-viewer/app.py
"""GE HTML Viewer — serves Great Expectations data quality reports from MinIO."""

import logging
import os
import re
from datetime import datetime

from flask import Flask, abort, render_template_string
from minio import Minio

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuration from environment
MINIO_ENDPOINT = os.environ.get("S3_ENDPOINT", "chi.tacc.chameleoncloud.org:7480")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "admin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "chatsentry_minio")
MINIO_SECURE = os.environ.get("S3_SECURE", "true").lower() == "true"
BUCKET_TRAINING = os.environ.get("BUCKET_TRAINING", "proj09_Data")

# Cache for HTML reports
_reports_cache: dict[str, dict[str, str]] = {}


def get_minio_client() -> Minio:
    """Create MinIO client (S3-compatible)."""
    return Minio(
        endpoint=MINIO_ENDPOINT,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=MINIO_SECURE,
        region="",
    )


def _parse_report_metadata(filename: str) -> dict[str, str]:
    """Extract stage and timestamp metadata from a GE report filename."""
    stem = filename[:-5] if filename.endswith(".html") else filename
    match = re.match(r"(?P<stage>[a-z0-9-]+)-(?P<timestamp>\d{8}-\d{6})$", stem)

    if not match:
        return {
            "stage": "report",
            "stage_label": "Report",
            "stage_class": "generic",
            "timestamp": "",
            "timestamp_label": "Unknown",
        }

    stage = match.group("stage")
    timestamp = match.group("timestamp")
    stage_class = stage if stage in {"before-cleaning", "after-cleaning"} else "generic"
    try:
        parsed = datetime.strptime(timestamp, "%Y%m%d-%H%M%S")
        timestamp_label = parsed.strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        timestamp_label = timestamp

    return {
        "stage": stage,
        "stage_label": stage.replace("-", " ").title(),
        "stage_class": stage_class,
        "timestamp": timestamp,
        "timestamp_label": timestamp_label,
    }


def fetch_reports() -> dict[str, dict[str, str]]:
    """Fetch all HTML reports from MinIO."""
    client = get_minio_client()
    reports = {}

    try:
        objects = client.list_objects(BUCKET_TRAINING, prefix="data-quality-report/", recursive=True)
        for obj in objects:
            if obj.object_name.endswith(".html"):
                response = client.get_object(BUCKET_TRAINING, obj.object_name)
                html_content = response.read().decode("utf-8")
                response.close()
                response.release_conn()
                filename = obj.object_name.split("/")[-1]
                metadata = _parse_report_metadata(filename)
                reports[filename] = {
                    "filename": filename,
                    "html": html_content,
                    **metadata,
                }
                logger.info("Fetched report: %s", filename)
    except Exception:
        logger.exception("Failed to fetch reports from MinIO")

    return dict(
        sorted(
            reports.items(),
            key=lambda item: item[1].get("timestamp", ""),
            reverse=True,
        )
    )


# Fetch reports on startup
_reports_cache = fetch_reports()


INDEX_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>ChatSentry GE Reports</title>
    <style>
        body { font-family: sans-serif; margin: 40px; }
        h1 { color: #333; }
        table { border-collapse: collapse; width: 100%; margin-top: 20px; }
        th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
        th { background: #f5f5f5; }
        a { color: #2e7d32; text-decoration: none; font-size: 16px; }
        a:hover { text-decoration: underline; }
        .refresh { margin-top: 20px; }
        button { padding: 10px 20px; background: #4CAF50; color: white; border: none; cursor: pointer; }
        .badge { display: inline-block; padding: 4px 8px; border-radius: 999px; font-size: 12px; font-weight: bold; }
        .badge.before-cleaning { background: #ffebee; color: #b71c1c; }
        .badge.after-cleaning { background: #e8f5e9; color: #1b5e20; }
        .badge.generic { background: #eceff1; color: #37474f; }
    </style>
</head>
<body>
    <h1>ChatSentry Data Quality Reports</h1>
    {% if reports %}
    <table>
        <tr>
            <th>Stage</th>
            <th>Generated</th>
            <th>Report</th>
        </tr>
        {% for report in reports %}
        <tr>
            <td><span class="badge {{ report.stage_class }}">{{ report.stage_label }}</span></td>
            <td>{{ report.timestamp_label }}</td>
            <td><a href="/report/{{ report.filename }}">{{ report.filename }}</a></td>
        </tr>
        {% endfor %}
    </table>
    {% else %}
    <p>No reports found. Generate reports by running compile_training_data.py</p>
    {% endif %}
    <div class="refresh">
        <form method="POST" action="/refresh">
            <button type="submit">Refresh Reports</button>
        </form>
    </div>
</body>
</html>
"""


@app.route("/")
def index():
    """List available reports."""
    return render_template_string(INDEX_TEMPLATE, reports=list(_reports_cache.values()))


@app.route("/report/<filename>")
def report(filename: str):
    """Serve HTML report."""
    if filename not in _reports_cache:
        abort(404)
    return _reports_cache[filename]["html"]


@app.route("/refresh", methods=["POST"])
def refresh():
    """Re-fetch reports from MinIO."""
    global _reports_cache
    _reports_cache = fetch_reports()
    return render_template_string(INDEX_TEMPLATE, reports=list(_reports_cache.values()))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
