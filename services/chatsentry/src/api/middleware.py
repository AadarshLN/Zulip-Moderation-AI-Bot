"""Text-cleaning middleware for FastAPI message ingestion.

Intercepts POST /messages and POST /flags, cleans text fields using
TextCleaner, persists to PostgreSQL with cleaned_text, and buffers
cleaned data for MinIO batch uploads (10K rows).

Maps to decisions D-11 through D-17.
"""

import io
import json
import logging
import uuid
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from src.data.text_cleaner import TextCleaner
from src.utils.config import config
from src.utils.db import get_db_connection
from src.utils.minio_client import get_minio_client

logger = logging.getLogger(__name__)

# Endpoints intercepted by the middleware (D-11)
_INTERCEPT_PATHS = {"/messages", "/flags"}
# MinIO batch size for cleaned data uploads (D-17)
_MINIO_BATCH_SIZE = 10_000


class TextCleaningMiddleware(BaseHTTPMiddleware):
    """Middleware that cleans text on incoming message/flag requests.

    For POST /messages and POST /flags:
    1. Reads request body once (Starlette limitation)
    2. Applies TextCleaner to text fields
    3. Persists message to PostgreSQL with cleaned_text (D-12)
    4. Buffers cleaned data for MinIO batch upload (D-16, D-17)
    5. Stores cleaned body on request.state for route handlers

    Attributes:
        cleaner: TextCleaner instance used for text processing.
        _minio_buffer: In-memory buffer for MinIO batch uploads.
    """

    def __init__(self, app: Any) -> None:
        """Initialize the middleware with a TextCleaner instance.

        Args:
            app: The ASGI application to wrap.
        """
        super().__init__(app)
        self.cleaner = TextCleaner()
        self._minio_buffer: list[dict[str, Any]] = []

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Intercept requests and clean text fields.

        Args:
            request: The incoming Starlette request.
            call_next: The next middleware/route handler.

        Returns:
            The response from the downstream handler.
        """
        # Only intercept POST requests to /messages and /flags
        if request.method != "POST" or request.url.path not in _INTERCEPT_PATHS:
            return await call_next(request)

        body: dict[str, Any] = {}
        try:
            # Read body ONCE — cannot re-read after this point
            body_bytes = await request.body()
            body = json.loads(body_bytes)

            # Clean text fields depending on endpoint
            cleaned_body = self._clean_payload(request.url.path, body)

            # Persist to PostgreSQL (D-12)
            if request.url.path == "/messages":
                self._persist_message(cleaned_body)
            elif request.url.path == "/flags":
                self._persist_flag(cleaned_body)

            # Buffer for MinIO batch upload (D-16, D-17)
            self._buffer_for_minio(cleaned_body)

            # Store cleaned_body on request.state for route handlers
            request.state.cleaned_body = cleaned_body

        except Exception:
            logger.exception("TextCleaningMiddleware failed, passing through")
            # On failure, pass the original body through so the route can still work
            request.state.cleaned_body = body

        return await call_next(request)

    def _clean_payload(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Apply TextCleaner to relevant fields in the payload.

        Args:
            path: The request path (/messages or /flags).
            body: Parsed JSON body.

        Returns:
            Copy of body with cleaned text fields added.
        """
        cleaned = dict(body)

        if path == "/messages":
            raw_text = body.get("text", "")
            cleaned["raw_text"] = raw_text
            cleaned["cleaned_text"] = self.cleaner.clean(raw_text)
        elif path == "/flags":
            reason = body.get("reason") or ""
            if reason:
                cleaned["reason_cleaned"] = self.cleaner.clean(reason)

        return cleaned

    def _persist_message(self, cleaned_body: dict[str, Any]) -> None:
        """Persist message to PostgreSQL with cleaned_text (D-12).

        Only persists for /messages endpoint.

        Args:
            cleaned_body: Payload with raw_text and cleaned_text fields.
        """
        if "raw_text" not in cleaned_body:
            # This is a /flags request, skip DB insert for messages table
            return

        message_id = str(uuid.uuid4())
        raw_text = cleaned_body["raw_text"]
        cleaned_text = cleaned_body["cleaned_text"]
        source = cleaned_body.get("source", "real")

        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                # Resolve user FK — use seed user for unknown/test IDs
                user_input = cleaned_body.get("user_id", "unknown")
                cur.execute(
                    "SELECT id FROM users WHERE username = %s OR id::text = %s LIMIT 1",
                    (user_input, user_input),
                )
                row = cur.fetchone()
                user_id = row[0] if row else self._get_default_user(cur)

                cur.execute(
                    "INSERT INTO messages (id, user_id, text, cleaned_text, source) "
                    "VALUES (%s, %s, %s, %s, %s)",
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

    def _persist_flag(self, cleaned_body: dict[str, Any]) -> None:
        """Persist flag to PostgreSQL.

        Args:
            cleaned_body: Payload with message_id, flagged_by, reason fields.
        """
        if "reason" not in cleaned_body or "message_id" not in cleaned_body:
            return

        flag_id = str(uuid.uuid4())
        reason = cleaned_body.get("reason_cleaned", cleaned_body["reason"])
        message_input = cleaned_body["message_id"]
        flagged_by_input = cleaned_body.get("flagged_by", "unknown")

        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                # Resolve message FK
                cur.execute(
                    "SELECT id FROM messages WHERE id::text = %s LIMIT 1",
                    (message_input,),
                )
                row = cur.fetchone()
                message_id = row[0] if row else None

                # Resolve flagged_by FK
                cur.execute(
                    "SELECT id FROM users WHERE username = %s OR id::text = %s LIMIT 1",
                    (flagged_by_input, flagged_by_input),
                )
                row = cur.fetchone()
                flagged_by = row[0] if row else self._get_default_user(cur)

                if message_id:
                    cur.execute(
                        "INSERT INTO flags (id, message_id, flagged_by, reason, source) "
                        "VALUES (%s, %s, %s, %s, 'real')",
                        (flag_id, message_id, flagged_by, reason),
                    )
                    conn.commit()
                    logger.info("Persisted flag %s to PostgreSQL", flag_id)
                else:
                    logger.warning(
                        "Skipped flag — message_id %s not found", message_input
                    )
        except Exception:
            conn.rollback()
            logger.exception("Failed to persist flag to PostgreSQL")
        finally:
            conn.close()

    @staticmethod
    def _get_default_user(cur) -> str:
        """Get a default user ID.

        Args:
            cur: Database cursor.

        Returns:
            UUID of a user.
        """
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

    def _buffer_for_minio(self, cleaned_body: dict[str, Any]) -> None:
        """Buffer cleaned data for MinIO batch upload (D-16, D-17).

        Args:
            cleaned_body: Payload with cleaned text fields.
        """
        if "cleaned_text" not in cleaned_body:
            return

        self._minio_buffer.append(
            {
                "user_id": cleaned_body.get("user_id"),
                "raw_text": cleaned_body.get("raw_text"),
                "cleaned_text": cleaned_body["cleaned_text"],
                "source": cleaned_body.get("source", "real"),
            }
        )

        if len(self._minio_buffer) >= _MINIO_BATCH_SIZE:
            self._flush_to_minio()

    def _flush_to_minio(self) -> None:
        """Upload buffered cleaned data to MinIO as a JSON lines file.

        Uploads to zulip-raw-messages/cleaned/ prefix (D-16).
        """
        if not self._minio_buffer:
            return

        try:
            client = get_minio_client()
            lines = "\n".join(json.dumps(row) for row in self._minio_buffer)
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
                len(self._minio_buffer),
                config.BUCKET_RAW,
                object_name,
            )
            self._minio_buffer.clear()
        except Exception:
            logger.exception("Failed to flush cleaned data to MinIO")
            # Don't raise — MinIO upload failure should not block API response
