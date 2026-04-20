"""One-time Qwen warmup and generation for the GPU service."""

import logging
import random

from src.data.prompts import LABEL_DISTRIBUTION, PROMPTS_BY_LABEL
from src.gpu_service.models import (
    GenerationRequest,
    GenerationResponse,
    TestResponse,
    TrainingResponse,
    TrainingRow,
)
from src.gpu_service.storage import stage_model_artifact

logger = logging.getLogger(__name__)

_model = None
_tokenizer = None


_HF_MODEL_ID = "Qwen/Qwen2.5-1.5B"


def _resolve_model_path() -> str:
    """Resolve model path: staged S3 snapshot dir or HuggingFace Hub ID."""

    try:
        staged = stage_model_artifact()
        # S3 stores in HF cache format — find the snapshot dir with config.json
        snapshots_dir = staged / "snapshots"
        if snapshots_dir.is_dir():
            for snap in snapshots_dir.iterdir():
                if (snap / "config.json").exists():
                    logger.info("Using staged snapshot: %s", snap)
                    return str(snap)
        # Flat layout — config.json at root
        if (staged / "config.json").exists():
            logger.info("Using staged flat model: %s", staged)
            return str(staged)
        logger.warning("Staged dir has no config.json, falling back to HF Hub")
    except Exception as exc:
        logger.warning("S3 staging failed (%s), falling back to HF Hub", exc)

    logger.info("Loading from HuggingFace Hub: %s", _HF_MODEL_ID)
    return _HF_MODEL_ID


def load_model_once() -> None:
    """Stage and load the Qwen model once at service startup."""

    global _model, _tokenizer
    if _model is not None and _tokenizer is not None:
        return

    from transformers import AutoModelForCausalLM, AutoTokenizer

    model_id = _resolve_model_path()
    logger.info("Loading Qwen from %s ...", model_id)
    _tokenizer = AutoTokenizer.from_pretrained(model_id)
    _model = AutoModelForCausalLM.from_pretrained(
        model_id,
        torch_dtype="auto",
        device_map="auto",
    )
    logger.info(
        "Model loaded: %.0fM params",
        sum(parameter.numel() for parameter in _model.parameters()) / 1e6,
    )


def _parse_numbered_list(text: str) -> list[str]:
    """Parse generated numbered text into message rows."""

    messages: list[str] = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith("example "):
            colon_pos = line.find(":")
            if colon_pos > 0:
                line = line[colon_pos + 1 :].strip()
        if line and line[0].isdigit():
            dot_pos = line.find(".")
            if 0 < dot_pos <= 3:
                line = line[dot_pos + 1 :].strip()
        if line and line[0] == '"' and line[-1] == '"':
            line = line[1:-1]
        if line:
            messages.append(line)
    return messages


def _generate_text(
    prompt: str,
    max_new_tokens: int = 250,
    temperature: float = 0.8,
) -> str:
    """Generate text from a prompt using the warm-loaded model."""

    import torch

    assert _model is not None and _tokenizer is not None, "Call load_model_once() first"
    device = next(_model.parameters()).device
    inputs = _tokenizer(prompt, return_tensors="pt")
    inputs = {key: value.to(device) for key, value in inputs.items()}
    with torch.no_grad():
        output = _model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            do_sample=True,
            top_p=0.9,
            repetition_penalty=1.15,
            pad_token_id=_tokenizer.eos_token_id,
        )
    return _tokenizer.decode(
        output[0][inputs["input_ids"].shape[1] :], skip_special_tokens=True
    )


def _build_training_rows(total_count: int) -> list[TrainingRow]:
    """Generate the labeled row payload for training mode."""

    rows: list[TrainingRow] = []
    for label_type, proportion in LABEL_DISTRIBUTION.items():
        target_count = max(1, int(total_count * proportion))
        prompt_obj = PROMPTS_BY_LABEL[label_type]
        raw = _generate_text(prompt_obj.prompt)
        for message in _parse_numbered_list(raw):
            rows.append(
                TrainingRow(
                    text=message,
                    is_suicide=int(prompt_obj.is_suicide),
                    is_toxicity=int(prompt_obj.is_toxicity),
                )
            )
            if len(rows) >= total_count or target_count <= 0:
                break
            target_count -= 1
        if len(rows) >= total_count:
            break
    return rows[:total_count]


def _build_test_rows(request: GenerationRequest) -> TestResponse:
    """Generate unlabeled test messages for live API exercise."""

    label = request.label or random.choice(["toxic", "suicide", "benign"])
    prompt_obj = PROMPTS_BY_LABEL[label]
    single_prompt = prompt_obj.prompt.replace("Generate 10 new", "Generate 1 new")
    raw = _generate_text(single_prompt, max_new_tokens=100, temperature=0.9)
    texts = _parse_numbered_list(raw)[: request.count]
    if not texts:
        fallback = raw.strip()
        texts = [fallback[:200]] if fallback else []
    return TestResponse(mode="test", texts=texts)


def generate(request: GenerationRequest) -> GenerationResponse:
    """Route generation by request mode."""

    if request.mode == "training":
        rows = _build_training_rows(request.count)
        return TrainingResponse(mode="training", rows=rows)
    if request.mode == "test":
        return _build_test_rows(request)
    raise ValueError(f"Unsupported mode: {request.mode!r}")
