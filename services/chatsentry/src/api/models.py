from typing import Optional

from pydantic import BaseModel, Field


class MessagePayload(BaseModel):
    text: str = Field(..., min_length=1, max_length=10000, description="Raw message text")
    user_id: str = Field(..., description="UUID or email of the sending user")
    source: str = Field(default="real", description="Data source: real or synthetic_hf")
    sender_email: Optional[str] = Field(default=None, description="Sender's email address")
    sender_full_name: Optional[str] = Field(default=None, description="Sender's display name")


class MessageResponse(BaseModel):
    status: str = "accepted"
    message_id: str
    raw_text: Optional[str] = Field(default=None, description="Original raw message text (D-13)")
    cleaned_text: Optional[str] = Field(default=None, description="Cleaned text after middleware processing (D-13)")


class FlagPayload(BaseModel):
    message_id: str = Field(..., description="UUID of the flagged message")
    flagged_by: str | None = Field(default=None, description="UUID of flagging user")
    reason: str | None = Field(default=None, description="Reason for flagging")


class FlagResponse(BaseModel):
    status: str = "accepted"
    flag_id: str
    reason_cleaned: Optional[str] = Field(default=None, description="Cleaned reason text after middleware processing")
