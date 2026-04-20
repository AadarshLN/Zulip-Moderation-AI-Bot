import io
import json
import logging
import uuid
from typing import Any

from fastapi import FastAPI

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


@app.middleware("http")
async def text_cleaning_middleware(request, call_next):
    """Clean text on POST /messages and POST /flags, persist to DB."""
    if request.method != "POST" or request.url.path not in ("/messages", "/flags"):
        return await call_next(request)

    cleaner = TextCleaner()
    body: dict[str, Any] = {}
    try:
        body_bytes = await request.body()

        async def receive() -> dict[str, Any]:
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
            user_input = cleaned_body.get("user_id", "unknown")
            cur.execute(
                "SELECT id FROM users WHERE username = %s OR id::text = %s LIMIT 1",
                (user_input, user_input),
            )
            row = cur.fetchone()
            user_id = row[0] if row else _get_default_user(cur)

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
    """Buffer cleaned data for MinIO batch upload."""
    if "cleaned_text" not in cleaned_body:
        return

    _minio_buffer.append(
        {
            "user_id": cleaned_body.get("user_id"),
            "raw_text": cleaned_body.get("raw_text"),
            "cleaned_text": cleaned_body["cleaned_text"],
            "source": cleaned_body.get("source", "real"),
        }
    )

    if len(_minio_buffer) >= _MINIO_BATCH_SIZE:
        _flush_to_minio()


def _flush_to_minio() -> None:
    """Upload buffered cleaned data to MinIO as JSON lines."""
    if not _minio_buffer:
        return

    try:
        client = get_minio_client()
        lines = "\n".join(json.dumps(row) for row in _minio_buffer)
        data = lines.encode("utf-8")
        object_name = f"zulip-raw-messages/cleaned/batch-{uuid.uuid4()}.jsonl"

        client.put_object(
            bucket_name=config.BUCKET_RAW,
            object_name=object_name,
            data=io.BytesIO(data),
            length=len(data),
            content_type="application/x-ndjson",
        )
        logger.info(
            "Uploaded %d cleaned records to MinIO %s/%s",
            len(_minio_buffer),
            config.BUCKET_RAW,
            object_name,
        )
        _minio_buffer.clear()
    except Exception:
        logger.exception("Failed to flush cleaned data to MinIO")


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


# TODO (Rishabh): /admin/flush is publicly accessible — add auth (e.g. admin API key header) before production.
@app.post("/admin/flush")
async def admin_flush():
    """Force-flush the MinIO buffer for testing."""
    _flush_to_minio()
    return {"status": "flushed", "buffer_remaining": len(_minio_buffer)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
