"""GPU service request/response Pydantic models."""

from typing import Literal

from pydantic import BaseModel, Field


class GenerationRequest(BaseModel):
    """Request contract for synthetic generation."""

    mode: Literal["training", "test"]
    count: int = Field(..., ge=1, le=500)
    label: Literal["toxic", "suicide", "benign"] | None = None


class TrainingRow(BaseModel):
    """Single labeled training row returned by the GPU service."""

    text: str = Field(..., min_length=1)
    is_suicide: int = Field(..., ge=0, le=1)
    is_toxicity: int = Field(..., ge=0, le=1)


class TrainingResponse(BaseModel):
    """Training-mode response payload."""

    mode: Literal["training"]
    rows: list[TrainingRow]


class TestResponse(BaseModel):
    """Test-mode response payload."""

    mode: Literal["test"]
    texts: list[str]


GenerationResponse = TrainingResponse | TestResponse
