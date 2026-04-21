"""
Send synthetic test messages to a Zulip stream.

Uses the GPU service to generate messages (toxic/suicide/benign mix).
Falls back to hardcoded samples if GPU service is unavailable.

Usage:
    python scripts/send_test_messages.py \
        --site https://zulip.129.114.26.93.nip.io \
        --email bot@zulip.example.com \
        --api-key <key> \
        --stream general \
        --topic "test" \
        --count 35 \
        --interval 3
"""

from __future__ import annotations

import argparse
import logging
import random
import time

import requests
import zulip

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Fallback messages when GPU service is unavailable
FALLBACK_MESSAGES = {
    "toxic": [
        "shut the fuck up you idiot",
        "you're a complete moron, go away",
        "i hate everyone in this channel",
        "this is absolute garbage, you suck",
        "what a stupid piece of shit comment",
        "you're worthless and everyone knows it",
        "get out of here you dumb asshole",
    ],
    "suicide": [
        "i don't see a reason to keep going anymore",
        "everything feels hopeless and i'm so tired",
        "i've been thinking about ending it all",
        "nobody would miss me if i was gone",
        "i just want the pain to stop permanently",
    ],
    "benign": [
        "hey everyone, how's the project going?",
        "can someone help me with this math problem?",
        "the weather looks great today!",
        "just finished reading a great book",
        "anyone want to grab lunch?",
        "i really enjoyed the lecture today",
        "what time does the meeting start?",
        "great work on the presentation everyone",
        "has anyone seen the latest movie?",
        "the coffee here is really good",
        "looking forward to the weekend",
        "does anyone have notes from yesterday?",
        "the new update looks really clean",
        "happy monday everyone!",
    ],
}


def get_message_from_gpu(gpu_url: str, api_key: str, label: str) -> str | None:
    """Try to get a message from the GPU service."""
    try:
        headers = {"X-API-Key": api_key} if api_key else {}
        response = requests.post(
            f"{gpu_url}/generate",
            json={"mode": "test", "count": 1, "label": label},
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        texts = data.get("texts", [])
        if texts and texts[0].strip():
            return texts[0].strip()
    except Exception as e:
        log.warning("GPU service unavailable (%s), using fallback", e)
    return None


def pick_label() -> str:
    """Weighted random label — more benign than toxic/suicide."""
    return random.choices(
        ["benign", "toxic", "suicide"],
        weights=[60, 30, 10],
        k=1,
    )[0]


def main():
    parser = argparse.ArgumentParser(description="Send synthetic test messages to Zulip")
    parser.add_argument("--site", required=True, help="Zulip server URL")
    parser.add_argument("--email", required=True, help="Bot email")
    parser.add_argument("--api-key", required=True, help="Bot API key")
    parser.add_argument("--stream", default="general", help="Stream to post to")
    parser.add_argument("--topic", default="synthetic-test", help="Topic")
    parser.add_argument("--count", type=int, default=35, help="Number of messages")
    parser.add_argument("--interval", type=float, default=3.0, help="Seconds between messages")
    parser.add_argument("--gpu-url", default=None, help="GPU service URL (optional)")
    parser.add_argument("--gpu-api-key", default="", help="GPU service API key")
    args = parser.parse_args()

    client = zulip.Client(site=args.site, email=args.email, api_key=args.api_key, insecure=True)

    # Verify connection
    result = client.get_profile()
    if result["result"] != "success":
        log.error("Failed to connect to Zulip: %s", result)
        return
    log.info("Connected as: %s", result["full_name"])

    sent = 0
    for i in range(args.count):
        label = pick_label()

        # Try GPU service first, fall back to hardcoded
        text = None
        if args.gpu_url:
            text = get_message_from_gpu(args.gpu_url, args.gpu_api_key, label)

        if not text:
            text = random.choice(FALLBACK_MESSAGES[label])

        result = client.send_message(
            {
                "type": "stream",
                "to": args.stream,
                "topic": args.topic,
                "content": text,
            }
        )

        if result["result"] == "success":
            sent += 1
            log.info("[%d/%d] [%s] %s", sent, args.count, label, text[:70])
        else:
            log.error("[%d/%d] Failed: %s", i + 1, args.count, result)

        if i < args.count - 1:
            time.sleep(args.interval)

    log.info("Done — sent %d/%d messages to #%s > %s", sent, args.count, args.stream, args.topic)


if __name__ == "__main__":
    main()
