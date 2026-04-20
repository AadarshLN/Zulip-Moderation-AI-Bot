import uuid

from fastapi import APIRouter, Request

from ..models import FlagPayload, FlagResponse

router = APIRouter()


@router.post("/flags", response_model=FlagResponse)
async def create_flag(payload: FlagPayload, request: Request):
    """Create a flag — middleware pre-cleans reason text.

    The TextCleaningMiddleware intercepts this request first, cleans
    the reason field, and stores the cleaned body on request.state.

    Args:
        payload: Validated flag payload.
        request: Starlette request with cleaned_body on state.

    Returns:
        FlagResponse with reason_cleaned field.
    """
    cleaned = getattr(request.state, "cleaned_body", {})
    reason_cleaned = cleaned.get("reason_cleaned", payload.reason)

    return FlagResponse(
        status="accepted",
        flag_id=str(uuid.uuid4()),
        reason_cleaned=reason_cleaned,
    )
