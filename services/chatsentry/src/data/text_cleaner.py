"""Shared text cleaning pipeline for online and batch processing paths.

Implements the TextCleaner class with configurable, ordered cleaning steps
per ONLINE-01 through ONLINE-06 and decisions D-05 through D-10.

Execution order (D-06):
    1. Unicode normalization (ftfy)
    2. Markdown strip
    3. URL extraction
    4. Emoji standardization
    5. PII scrubbing
"""

import logging
import re
import warnings
from dataclasses import dataclass, field
from typing import Callable

import emoji
import ftfy
from bs4 import MarkupResemblesLocatorWarning
from markdownify import markdownify

warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PII regex patterns (module-level for testability, per D-07)
# ---------------------------------------------------------------------------

EMAIL_PATTERN: re.Pattern[str] = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
"""Matches standard email addresses. Replaced with [EMAIL]."""

PHONE_PATTERN: re.Pattern[str] = re.compile(r"(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")
"""Matches US-style phone numbers. Replaced with [PHONE]."""

USERNAME_PATTERN: re.Pattern[str] = re.compile(r"@\w+")
"""Matches @username mentions. Replaced with [USER]."""

# ---------------------------------------------------------------------------
# Individual cleaning step functions
# ---------------------------------------------------------------------------


def fix_unicode(text: str) -> str:
    """Normalize Unicode text using ftfy (ONLINE-05, D-05 step 1).

    Fixes common encoding issues such as mojibake (e.g., cafÃ© -> café).

    Args:
        text: Raw input text potentially containing encoding artifacts.

    Returns:
        Text with Unicode issues resolved.
    """
    return ftfy.fix_text(text)


def strip_markdown(text: str) -> str:
    """Remove HTML tags and Markdown syntax markers (ONLINE-01).

    Two-phase approach (research findings):
        1. Strip HTML tags via markdownify
        2. Remove Markdown syntax markers (*, _, ~, `)
        3. Collapse whitespace

    Args:
        text: Text that may contain HTML or Markdown formatting.

    Returns:
        Plain text with formatting removed.
    """
    # Phase 1: strip HTML tags
    text = markdownify(text, strip=["a", "b", "i", "img", "code", "pre", "p", "div", "span"])
    # Phase 2: unescape backslash-escaped Markdown markers (markdownify escapes * _ ~ `)
    text = re.sub(r"\\([*_~`])", r"\1", text)
    # Phase 3: strip Markdown syntax markers
    text = re.sub(r"[*_~`]+", "", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_urls(text: str) -> str:
    """Replace URLs with [URL] placeholder (ONLINE-03, D-08).

    Args:
        text: Text potentially containing HTTP/HTTPS URLs.

    Returns:
        Text with all URLs replaced by [URL].
    """
    return re.sub(r"https?://\S+", "[URL]", text)


def standardize_emojis(text: str) -> str:
    """Convert Unicode emojis to :shortcode: format (ONLINE-02, D-09).

    Args:
        text: Text potentially containing Unicode emoji characters.

    Returns:
        Text with emojis converted to :shortcode: notation.
    """
    return emoji.demojize(text, delimiters=(":", ":"))


def scrub_pii(text: str) -> str:
    """Replace personally identifiable information with placeholders (ONLINE-04, D-07).

    Scrubs in order: email, phone, username.

    Args:
        text: Text potentially containing PII (emails, phone numbers, usernames).

    Returns:
        Text with PII replaced by [EMAIL], [PHONE], and [USER] placeholders.
    """
    text = EMAIL_PATTERN.sub("[EMAIL]", text)
    text = PHONE_PATTERN.sub("[PHONE]", text)
    text = USERNAME_PATTERN.sub("[USER]", text)
    return text


# ---------------------------------------------------------------------------
# TextCleaner pipeline class
# ---------------------------------------------------------------------------


@dataclass
class TextCleaner:
    """Shared text cleaning pipeline for online and batch paths (ONLINE-06).

    Steps executed in order per D-06:
        1. Unicode normalization (ftfy)
        2. Markdown strip
        3. URL extraction
        4. Emoji standardization
        5. PII scrubbing

    Attributes:
        steps: Ordered list of callable cleaning functions.
    """

    steps: list[Callable[[str], str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Populate default steps if none provided."""
        if not self.steps:
            self.steps = [
                fix_unicode,
                strip_markdown,
                extract_urls,
                standardize_emojis,
                scrub_pii,
            ]

    def clean(self, text: str) -> str:
        """Apply all cleaning steps sequentially.

        Args:
            text: Raw input text.

        Returns:
            Fully cleaned text after all pipeline steps.
        """
        for step in self.steps:
            text = step(text)
        return text
