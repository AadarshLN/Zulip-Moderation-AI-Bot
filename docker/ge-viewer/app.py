# docker/ge-viewer/app.py
"""GE HTML Viewer — serves Great Expectations data quality reports from MinIO."""

import logging
import os

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
_reports_cache: dict[str, str] = {}


def get_minio_client() -> Minio:
    """Create MinIO client (S3-compatible)."""
    return Minio(
        endpoint=MINIO_ENDPOINT,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=MINIO_SECURE,
        region="",
    )


def fetch_reports() -> dict[str, str]:
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
                reports[filename] = html_content
                logger.info("Fetched report: %s", filename)
    except Exception:
        logger.exception("Failed to fetch reports from MinIO")

    return reports


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
        ul { list-style: none; padding: 0; }
        li { margin: 10px 0; }
        a { color: #4CAF50; text-decoration: none; font-size: 18px; }
        a:hover { text-decoration: underline; }
        .refresh { margin-top: 20px; }
        button { padding: 10px 20px; background: #4CAF50; color: white; border: none; cursor: pointer; }
    </style>
</head>
<body>
    <h1>ChatSentry Data Quality Reports</h1>
    {% if reports %}
    <ul>
        {% for filename in reports %}
        <li><a href="/report/{{ filename }}">{{ filename }}</a></li>
        {% endfor %}
    </ul>
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
    return render_template_string(INDEX_TEMPLATE, reports=list(_reports_cache.keys()))


@app.route("/report/<filename>")
def report(filename: str):
    """Serve HTML report."""
    if filename not in _reports_cache:
        abort(404)
    return _reports_cache[filename]


@app.route("/refresh", methods=["POST"])
def refresh():
    """Re-fetch reports from MinIO."""
    global _reports_cache
    _reports_cache = fetch_reports()
    return render_template_string(INDEX_TEMPLATE, reports=list(_reports_cache.keys()))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
