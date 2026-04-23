import io
import json
import logging
import uuid
from typing import Any

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from src.data.text_cleaner import TextCleaner
from src.utils.config import config
from src.utils.db import get_db_connection
from src.utils.minio_client import get_minio_client

from .routes import dashboard, flags, messages

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Shared MinIO buffer — accessed by middleware and flush endpoint
_minio_buffer: list[dict[str, Any]] = []
_MINIO_BATCH_SIZE = 10_000

app = FastAPI(title="ChatSentry API", version="0.1.0")
Instrumentator().instrument(app).expose(app)


@app.middleware("http")
async def text_cleaning_middleware(request, call_next):
    """Clean text on POST /messages and POST /flags, persist to DB."""
    if request.method != "POST" or request.url.path not in ("/messages", "/flags"):
        return await call_next(request)

    cleaner = TextCleaner()
    body: dict[str, Any] = {}
    try:
        body_bytes = await request.body()

        async def receive() -> dict:
            return {"type": "http.request", "body": body_bytes, "more_body": False}

        request._receive = receive  # type: ignore[attr-defined]
        body = json.loads(body_bytes)
        cleaned = dict(body)

        if request.url.path == "/messages":
            raw_text = body.get("text", "").strip()
            if not raw_text:
                # Skip empty messages
                request.state.cleaned_body = body  # type: ignore[attr-defined]
                return await call_next(request)
            cleaned["raw_text"] = raw_text
            cleaned["cleaned_text"] = cleaner.clean(raw_text)
            _persist_message(cleaned)
            _buffer_for_minio(cleaned)
        elif request.url.path == "/flags":
            reason = body.get("reason") or ""
            if reason:
                cleaned["reason_cleaned"] = cleaner.clean(reason)
            _persist_flag(cleaned)

        request.state.cleaned_body = cleaned  # type: ignore[attr-defined]
    except Exception:
        logger.exception("TextCleaningMiddleware failed, passing through")
        request.state.cleaned_body = body  # type: ignore[attr-defined]

    return await call_next(request)


app.include_router(messages.router)
app.include_router(flags.router)
app.include_router(dashboard.router)


def _get_default_user(cur) -> str:
    """Get a default user ID."""
    cur.execute("SELECT id FROM users WHERE username = 'test_user' LIMIT 1")
    row = cur.fetchone()
    if row:
        return row[0]
    # Fallback to any available user
    cur.execute("SELECT id FROM users LIMIT 1")
    row = cur.fetchone()
    if row:
        return row[0]
    raise ValueError("No users in database — run seed data first")


def _persist_message(cleaned_body: dict[str, Any]) -> None:
    """Persist message to PostgreSQL."""
    if "raw_text" not in cleaned_body:
        return

    message_id = str(uuid.uuid4())
    raw_text = cleaned_body["raw_text"]
    cleaned_text = cleaned_body["cleaned_text"]
    source = cleaned_body.get("source", "real")
    if source not in ("real", "synthetic"):
        source = "real"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            sender_email = cleaned_body.get("sender_email")

            if sender_email:
                # Upsert user by email — creates on first seen, stable across messages
                cur.execute(
                    """
                    INSERT INTO users (id, username, email, source)
                    VALUES (gen_random_uuid(), %s, %s, 'real')
                    ON CONFLICT (username) DO UPDATE SET email = EXCLUDED.email
                    RETURNING id
                    """,
                    (sender_email, sender_email),
                )
                user_id = cur.fetchone()[0]
            else:
                # Fallback: look up by user_id field
                user_input = cleaned_body.get("user_id", "unknown")
                cur.execute(
                    "SELECT id FROM users WHERE username = %s OR id::text = %s LIMIT 1",
                    (user_input, user_input),
                )
                row = cur.fetchone()
                if row:
                    user_id = row[0]
                else:
                    cur.execute(
                        "INSERT INTO users (id, username, source) VALUES (gen_random_uuid(), %s, 'real') RETURNING id",
                        (user_input,),
                    )
                    user_id = cur.fetchone()[0]

            cur.execute(
                "INSERT INTO messages (id, user_id, text, cleaned_text, source) VALUES (%s, %s, %s, %s, %s)",
                (message_id, user_id, raw_text, cleaned_text, source),
            )
        conn.commit()
        cleaned_body["_message_id"] = message_id
        logger.info("Persisted message %s to PostgreSQL", message_id)
    except Exception:
        conn.rollback()
        logger.exception("Failed to persist message to PostgreSQL")
        raise
    finally:
        conn.close()


def _persist_flag(cleaned_body: dict[str, Any]) -> None:
    """Persist flag to PostgreSQL."""
    if "reason" not in cleaned_body or "message_id" not in cleaned_body:
        return

    flag_id = str(uuid.uuid4())
    reason = cleaned_body.get("reason_cleaned", cleaned_body["reason"])
    message_input = cleaned_body["message_id"]
    flagged_by_input = cleaned_body.get("flagged_by", "unknown")

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM messages WHERE id::text = %s LIMIT 1",
                (message_input,),
            )
            row = cur.fetchone()
            message_id = row[0] if row else None

            cur.execute(
                "SELECT id FROM users WHERE username = %s OR id::text = %s LIMIT 1",
                (flagged_by_input, flagged_by_input),
            )
            row = cur.fetchone()
            flagged_by = row[0] if row else _get_default_user(cur)

            if message_id:
                cur.execute(
                    "INSERT INTO flags (id, message_id, flagged_by, reason, source) VALUES (%s, %s, %s, %s, 'real')",
                    (flag_id, message_id, flagged_by, reason),
                )
                conn.commit()
                logger.info("Persisted flag %s to PostgreSQL", flag_id)
            else:
                logger.warning("Skipped flag — message_id %s not found", message_input)
    except Exception:
        conn.rollback()
        logger.exception("Failed to persist flag to PostgreSQL")
    finally:
        conn.close()


def _buffer_for_minio(cleaned_body: dict[str, Any]) -> None:
    """Buffer message IDs for MinIO batch export."""
    if "cleaned_text" not in cleaned_body:
        return

    _minio_buffer.append(
        {
            "_message_id": cleaned_body.get("_message_id"),
            "raw_text": cleaned_body.get("raw_text"),
            "cleaned_text": cleaned_body["cleaned_text"],
            "source": cleaned_body.get("source", "real"),
        }
    )

    if len(_minio_buffer) >= _MINIO_BATCH_SIZE:
        _flush_to_minio()


def _flush_to_minio() -> None:
    """Export labeled messages from DB to MinIO as training data (JSONL).

    Queries messages joined with their labels so is_toxicity/is_suicide are
    included. Only messages that have been seen since the last flush (tracked
    via _minio_buffer message IDs) are exported.
    """
    if not _minio_buffer:
        return

    # Collect message IDs buffered since last flush
    message_ids = [row["_message_id"] for row in _minio_buffer if row.get("_message_id")]

    try:
        rows: list[dict[str, Any]] = []

        if message_ids:
            conn = get_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            m.id,
                            m.text         AS raw_text,
                            m.cleaned_text,
                            m.is_toxicity,
                            m.is_suicide,
                            m.source
                        FROM messages m
                        WHERE m.id = ANY(%s::uuid[])
                        """,
                        (message_ids,),
                    )
                    for rec in cur.fetchall():
                        rows.append({
                            "message_id": str(rec[0]),
                            "raw_text": rec[1],
                            "cleaned_text": rec[2],
                            "is_toxicity": rec[3] or False,
                            "is_suicide": rec[4] or False,
                            "source": rec[5],
                        })
            finally:
                conn.close()
        else:
            # Fallback: use buffer data (no labels available)
            for row in _minio_buffer:
                rows.append({
                    "raw_text": row.get("raw_text"),
                    "cleaned_text": row.get("cleaned_text"),
                    "is_toxicity": False,
                    "is_suicide": False,
                    "source": row.get("source", "real"),
                })

        client = get_minio_client()
        lines = "\n".join(json.dumps(row) for row in rows)
        data = lines.encode("utf-8")
        object_name = f"zulip-training-data/batch-{uuid.uuid4()}.jsonl"

        client.put_object(
            bucket_name=config.BUCKET_RAW,
            object_name=object_name,
            data=io.BytesIO(data),
            length=len(data),
            content_type="application/x-ndjson",
        )
        logger.info(
            "Uploaded %d training records to MinIO %s/%s",
            len(rows),
            config.BUCKET_RAW,
            object_name,
        )
        _minio_buffer.clear()
    except Exception:
        logger.exception("Failed to flush training data to MinIO")


@app.get("/", include_in_schema=False)
async def root():
    return {
        "service": "ChatSentry API",
        "version": "0.1.0",
        "docs": "/docs",
        "health": "/health",
        "dashboard": "/dashboard",
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


def _trigger_compile_job() -> str:
    """Delete any existing compile-training-data job and create a fresh one."""
    try:
        from kubernetes import client as k8s_client, config as k8s_config
        k8s_config.load_incluster_config()
        batch = k8s_client.BatchV1Api()
        namespace = "platform"
        job_name = "compile-training-data"

        # Delete existing job if present
        try:
            batch.delete_namespaced_job(
                name=job_name,
                namespace=namespace,
                body=k8s_client.V1DeleteOptions(propagation_policy="Foreground"),
            )
            logger.info("Deleted existing compile job")
        except k8s_client.exceptions.ApiException as e:
            if e.status != 404:
                raise

        # Build job spec mirroring compile-data-job.yaml
        env = [
            k8s_client.V1EnvVar(
                name="DATABASE_URL",
                value_from=k8s_client.V1EnvVarSource(
                    secret_key_ref=k8s_client.V1SecretKeySelector(name="chatsentry-credentials", key="DATABASE_URL")
                ),
            ),
            k8s_client.V1EnvVar(name="S3_ENDPOINT", value="chi.tacc.chameleoncloud.org:7480"),
            k8s_client.V1EnvVar(name="S3_SECURE", value="true"),
            k8s_client.V1EnvVar(name="MINIO_ENDPOINT", value="chi.tacc.chameleoncloud.org:7480"),
            k8s_client.V1EnvVar(
                name="MINIO_ACCESS_KEY",
                value_from=k8s_client.V1EnvVarSource(
                    secret_key_ref=k8s_client.V1SecretKeySelector(name="chatsentry-credentials", key="S3_ACCESS_KEY")
                ),
            ),
            k8s_client.V1EnvVar(
                name="MINIO_SECRET_KEY",
                value_from=k8s_client.V1EnvVarSource(
                    secret_key_ref=k8s_client.V1SecretKeySelector(name="chatsentry-credentials", key="S3_SECRET_KEY")
                ),
            ),
            k8s_client.V1EnvVar(name="MINIO_SECURE", value="true"),
            k8s_client.V1EnvVar(name="GPU_SERVICE_URL", value="http://gpu-service.platform.svc.cluster.local:8001"),
            k8s_client.V1EnvVar(
                name="GPU_SERVICE_API_KEY",
                value_from=k8s_client.V1EnvVarSource(
                    secret_key_ref=k8s_client.V1SecretKeySelector(name="gpu-service-credentials", key="GPU_SERVICE_API_KEY")
                ),
            ),
        ]

        job = k8s_client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=k8s_client.V1ObjectMeta(name=job_name, namespace=namespace),
            spec=k8s_client.V1JobSpec(
                backoff_limit=0,
                ttl_seconds_after_finished=3600,
                template=k8s_client.V1PodTemplateSpec(
                    spec=k8s_client.V1PodSpec(
                        restart_policy="Never",
                        containers=[
                            k8s_client.V1Container(
                                name="compile",
                                image="kichanitish/chatsentry-data:latest",
                                image_pull_policy="Always",
                                env=env,
                                resources=k8s_client.V1ResourceRequirements(
                                    requests={"cpu": "500m", "memory": "2Gi"},
                                    limits={"cpu": "2000m", "memory": "4Gi"},
                                ),
                            )
                        ],
                    )
                ),
            ),
        )
        batch.create_namespaced_job(namespace=namespace, body=job)
        logger.info("Triggered compile-training-data job")
        return "triggered"
    except Exception as e:
        logger.warning("Could not trigger compile job: %s", e)
        return f"skipped: {e}"


@app.post("/admin/flush")
async def admin_flush():
    """Force-flush the MinIO buffer and trigger the training data compile job."""
    _flush_to_minio()
    compile_status = _trigger_compile_job()
    return {
        "status": "flushed",
        "buffer_remaining": len(_minio_buffer),
        "compile_job": compile_status,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
