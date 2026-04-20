"""Dashboard routes for labeling live messages."""

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from src.utils.db import get_db_connection

logger = logging.getLogger(__name__)

router = APIRouter()

templates = Jinja2Templates(directory="src/api/templates")


class LabelRequest(BaseModel):
    """Request model for labeling a message."""

    message_id: str
    is_toxicity: bool = False
    is_suicide: bool = False


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request, page: int = Query(1, ge=1)):
    """Render the labeling dashboard with unlabeled live messages."""
    limit = 20
    offset = (page - 1) * limit

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.id, m.text, m.cleaned_text, m.created_at
                FROM messages m
                WHERE m.source = 'real'
                  AND NOT EXISTS (
                    SELECT 1 FROM moderation mod WHERE mod.message_id = m.id
                  )
                ORDER BY m.created_at DESC
                LIMIT %s OFFSET %s
                """,
                (limit + 1, offset),
            )
            rows = cur.fetchall()

            has_more = len(rows) > limit
            rows = rows[:limit]

            messages = []
            for row in rows:
                messages.append(
                    {
                        "id": str(row[0]),
                        "text": row[1],
                        "cleaned_text": row[2],
                        "created_at": row[3],
                    }
                )
    finally:
        conn.close()

    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "messages": messages, "page": page, "has_more": has_more},
    )


@router.post("/dashboard/label")
async def save_label(label: LabelRequest):
    """Save labels for a message and create moderation entry."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE messages 
                SET is_toxicity = %s, is_suicide = %s
                WHERE id = %s
                """,
                (label.is_toxicity, label.is_suicide, label.message_id),
            )

            cur.execute(
                """
                INSERT INTO moderation (message_id, action, confidence, source)
                VALUES (%s, %s, %s, %s)
                """,
                (label.message_id, "labeled", 1.0, "real"),
            )

        conn.commit()
        logger.info(
            "Labeled message %s: toxicity=%s, suicide=%s",
            label.message_id,
            label.is_toxicity,
            label.is_suicide,
        )
        return {"status": "ok", "message_id": label.message_id}
    except Exception:
        conn.rollback()
        logger.exception("Failed to save label")
        raise HTTPException(status_code=500, detail="Failed to save label")
    finally:
        conn.close()
