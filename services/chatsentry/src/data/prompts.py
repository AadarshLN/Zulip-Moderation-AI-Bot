"""Prompt templates for synthetic Zulip message generation.

Uses few-shot prompting with real examples from the dataset.
Local model (Qwen2.5-1.5B) — no HuggingFace API needed.

Per D-11: Oversample minority classes (toxic/suicide).
Per D-13: Prompt-guided labeling — labels assigned from prompt instruction.
"""

from dataclasses import dataclass
from typing import Literal

LabelType = Literal["toxic", "suicide", "benign"]


@dataclass(frozen=True)
class GenerationPrompt:
    prompt: str
    is_suicide: bool
    is_toxicity: bool
    label_type: LabelType


# Real examples from the dataset (extracted from combined_dataset.csv)
TOXIC_EXAMPLES = [
    "You are a biased idiot, YOU ARE A FUCKING JOKE!",
    "How dare you delete this, you piece of shit.",
    "thanks for ruining fun, dick faggot",
]

SUICIDE_EXAMPLES = [
    "When I am alone, I think about how I want to end it.",
    "I am so done and I have nothing worth living for.",
    "I just tried suffocating myself with a pillow.",
]

BENIGN_EXAMPLES = [
    "Has anyone tried the new deployment pipeline?",
    "Can someone review my pull request?",
    "Good morning everyone! Hope you had a great weekend.",
]


def _build_few_shot_prompt(label: str, examples: list[str], count: int = 10) -> str:
    """Build a few-shot generation prompt.

    Args:
        label: Description of what to generate.
        examples: Real examples from the dataset.
        count: Number of messages to generate.

    Returns:
        Formatted prompt string.
    """
    example_block = "\n".join(f"Example {i + 1}: {ex}" for i, ex in enumerate(examples))
    return (
        f"Generate {count} new chat messages that are {label}. "
        f"Here are real examples:\n\n{example_block}\n\n"
        f"Generate {count} new messages similar to these "
        f"(one per line, numbered 1-{count}):\n1."
    )


TOXIC_PROMPT = GenerationPrompt(
    prompt=_build_few_shot_prompt(
        "toxic (insults, threats, hate speech)", TOXIC_EXAMPLES
    ),
    is_suicide=False,
    is_toxicity=True,
    label_type="toxic",
)

SUICIDE_PROMPT = GenerationPrompt(
    prompt=_build_few_shot_prompt(
        "expressing suicidal thoughts or self-harm", SUICIDE_EXAMPLES
    ),
    is_suicide=True,
    is_toxicity=False,
    label_type="suicide",
)

BENIGN_PROMPT = GenerationPrompt(
    prompt=_build_few_shot_prompt(
        "normal, friendly chat messages (project updates, code reviews, "
        "casual conversation, asking for help)",
        BENIGN_EXAMPLES,
    ),
    is_suicide=False,
    is_toxicity=False,
    label_type="benign",
)

# Label distribution for ~10K rows (D-10, D-11: oversample minorities)
LABEL_DISTRIBUTION = {
    "toxic": 0.30,
    "suicide": 0.30,
    "benign": 0.40,
}

PROMPTS_BY_LABEL: dict[LabelType, GenerationPrompt] = {
    "toxic": TOXIC_PROMPT,
    "suicide": SUICIDE_PROMPT,
    "benign": BENIGN_PROMPT,
}
