import uuid

from fastapi import APIRouter, Request

from ..models import MessagePayload, MessageResponse

router = APIRouter()


@router.post("/messages", response_model=MessageResponse)
async def create_message(payload: MessagePayload, request: Request):
    """Create a message — middleware pre-cleans text and persists to DB.

    The TextCleaningMiddleware intercepts this request first, cleans the
    text, persists to PostgreSQL, and stores the cleaned body on
    request.state.cleaned_body.

    Args:
        payload: Validated message payload.
        request: Starlette request with cleaned_body on state.

    Returns:
        MessageResponse with raw_text and cleaned_text (D-13).
    """
    cleaned = getattr(request.state, "cleaned_body", {})
    message_id = cleaned.get("_message_id", str(uuid.uuid4()))

    return MessageResponse(
        status="accepted",
        message_id=message_id,
        raw_text=cleaned.get("raw_text", payload.text),
        cleaned_text=cleaned.get("cleaned_text", payload.text),
    )
