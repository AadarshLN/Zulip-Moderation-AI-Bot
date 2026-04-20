"""ChatSentry GPU service app."""

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader

from src.gpu_service.models import GenerationRequest, GenerationResponse
from src.gpu_service.runtime import generate, load_model_once
from src.utils.config import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _verify_api_key(api_key: str | None = Security(_api_key_header)) -> None:
    """Reject missing or invalid API keys when GPU auth is configured."""

    if config.GPU_SERVICE_API_KEY and api_key != config.GPU_SERVICE_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Stage and warm-load the model once at startup."""

    logger.info("GPU service startup: staging and loading model...")
    load_model_once()
    logger.info("GPU service ready.")
    yield


app = FastAPI(title="ChatSentry GPU Service", version="0.1.0", lifespan=lifespan)


@app.post(
    "/generate",
    response_model=GenerationResponse,
    dependencies=[Depends(_verify_api_key)],
)
async def generate_endpoint(request: GenerationRequest) -> GenerationResponse:
    """Generate synthetic text for training or test usage."""

    try:
        return generate(request)
    except Exception as exc:
        logger.exception("Generation failed")
        raise HTTPException(
            status_code=500,
            detail="Generation failed — check GPU service logs",
        ) from exc


@app.get("/health")
async def health() -> dict[str, str]:
    """Return service liveness status."""

    return {"status": "ok"}
