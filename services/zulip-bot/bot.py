"""
Zulip Moderation Bot

Listens to all messages via Zulip's event queue, sends each message to the
ChatSentry API for storage and the inference service for scoring, then takes
action based on the inference service's recommended action:

  HIDE_AND_STRIKE  → delete the message (high toxicity)
  WARN_AND_OBSCURE → post to #moderation stream for human review (medium toxicity)
  ALERT_ADMIN      → post to #moderation stream for human review (self-harm)
  ALLOW            → no action

All decisions are recorded back to ChatSentry via POST /flags.
"""

import logging
import os
import sys
import uuid

import requests
import zulip

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── Config from environment ────────────────────────────────────────────────────

ZULIP_SITE = os.environ["ZULIP_SITE"]  # e.g. https://zulip.129.114.26.93.nip.io
ZULIP_EMAIL = os.environ["ZULIP_BOT_EMAIL"]  # bot email from Zulip admin
ZULIP_API_KEY = os.environ["ZULIP_BOT_API_KEY"]  # bot API key from Zulip admin

CHATSENTRY_URL = os.environ.get("CHATSENTRY_URL", "http://chatsentry.platform.svc.cluster.local:8000")
INFERENCE_URL = os.environ.get("INFERENCE_URL", "http://inference.platform.svc.cluster.local:8000")

# Moderation stream — flagged messages are posted here for human review
MODERATION_STREAM = os.environ.get("MODERATION_STREAM", "moderation")

# ── Zulip client ───────────────────────────────────────────────────────────────

client = zulip.Client(
    site=ZULIP_SITE,
    email=ZULIP_EMAIL,
    api_key=ZULIP_API_KEY,
    insecure=True,  # self-signed TLS cert on internal deployment
)


# ── Helpers ────────────────────────────────────────────────────────────────────


def send_to_chatsentry(text: str, msg: dict) -> dict | None:
    """POST message to ChatSentry for cleaning + storage. Returns response JSON."""
    try:
        resp = requests.post(
            f"{CHATSENTRY_URL}/messages",
            json={
                "text": text,
                "user_id": msg.get("sender_email", str(uuid.uuid5(uuid.NAMESPACE_DNS, str(msg.get("sender_id", 0))))),
                "source": "real",
                "sender_email": msg.get("sender_email"),
                "sender_full_name": msg.get("sender_full_name"),
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error("ChatSentry /messages failed: %s", e)
        return None


def get_moderation(message_id: str, msg: dict) -> dict | None:
    """POST to inference /moderate, return the full response dict.
    Returns None on error so the message is not incorrectly actioned."""
    try:
        resp = requests.post(
            f"{INFERENCE_URL}/moderate",
            json={
                "message_id": message_id,
                "token": "",
                "metadata": {
                    "stream_id": msg.get("stream_id", 0),
                    "topic": msg.get("subject", ""),
                },
                "message": {
                    "sender_email": msg.get("sender_email", ""),
                    "raw_text": msg.get("content", ""),
                    "cleaned_text": msg.get("content", ""),
                },
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error("Inference /moderate failed: %s", e)
        return None


def record_moderation(message_id: str, reason: str, score: float) -> None:
    """Record the moderation action back to ChatSentry."""
    try:
        requests.post(
            f"{CHATSENTRY_URL}/flags",
            json={
                "message_id": message_id,
                "flagged_by": "moderation-bot",
                "reason": f"{reason} (score={score:.2f})",
            },
            timeout=10,
        )
    except Exception as e:
        log.error("ChatSentry /flags failed: %s", e)


def delete_message(message_id: int) -> None:
    result = client.delete_message(message_id)
    if result["result"] != "success":
        log.error("Failed to delete message %d: %s", message_id, result)
    else:
        log.info("Deleted message %d", message_id)


def flag_for_review(event: dict, action: str, score: float) -> None:
    """Post a moderation alert to the #moderation stream."""
    sender = event.get("sender_full_name", "unknown")
    stream = event.get("display_recipient", "unknown")
    topic = event.get("subject", "unknown")
    text = event.get("content", "")
    msg_id = event.get("id")

    alert = (
        f"⚠️ **Flagged message** (action: {action}, score: {score:.2f})\n"
        f"**From:** {sender}\n"
        f"**Stream:** {stream} > {topic}\n"
        f"**Message ID:** {msg_id}\n"
        f"**Content:** {text[:500]}"
    )
    result = client.send_message(
        {
            "type": "stream",
            "to": MODERATION_STREAM,
            "topic": "Flagged Messages",
            "content": alert,
        }
    )
    if result["result"] != "success":
        log.error("Failed to post to moderation stream: %s", result)
    else:
        log.info("Flagged message %d to #%s (action=%s, score=%.2f)", msg_id, MODERATION_STREAM, action, score)


# ── Event handler ──────────────────────────────────────────────────────────────


def handle_event(event: dict) -> None:
    """Called for every event received from Zulip."""
    if event.get("type") != "message":
        return

    msg = event["message"]

    # Ignore bot's own messages and messages in the moderation stream
    if msg.get("sender_email") == ZULIP_EMAIL:
        return
    if msg.get("display_recipient") == MODERATION_STREAM:
        return

    text = msg.get("content", "")
    zulip_msg_id = msg["id"]
    zulip_user_id = msg["sender_id"]

    log.info("Processing message %d from user %d", zulip_msg_id, zulip_user_id)

    # 1. Store in ChatSentry
    cs_response = send_to_chatsentry(text, msg)
    message_id = cs_response["message_id"] if cs_response else str(uuid.uuid4())

    # 2. Get moderation decision from inference service
    moderation = get_moderation(message_id, msg)
    if moderation is None:
        log.warning("No moderation response for message %d — skipping", zulip_msg_id)
        return

    action = moderation.get("action", "ALLOW")
    scores = moderation.get("scores", {})
    toxicity = scores.get("toxicity", 0.0)
    self_harm = scores.get("self_harm", 0.0)
    log.info("Message %d → action=%s toxicity=%.2f self_harm=%.2f", zulip_msg_id, action, toxicity, self_harm)

    # 3. Act on the inference service's recommended action
    if action == "HIDE_AND_STRIKE":
        log.info("Action HIDE_AND_STRIKE — deleting message %d", zulip_msg_id)
        delete_message(zulip_msg_id)
        record_moderation(message_id, "auto-deleted", toxicity)

    elif action in ("WARN_AND_OBSCURE", "ALERT_ADMIN"):
        score = self_harm if action == "ALERT_ADMIN" else toxicity
        log.info("Action %s — flagging message %d for review", action, zulip_msg_id)
        flag_for_review(msg, action, score)
        record_moderation(message_id, action.lower(), score)

    else:
        log.debug("Action ALLOW — no action taken for message %d", zulip_msg_id)


# ── Main loop ──────────────────────────────────────────────────────────────────


def main() -> None:
    log.info("Zulip moderation bot starting")
    log.info("Site: %s | Bot: %s", ZULIP_SITE, ZULIP_EMAIL)
    log.info("Moderation stream: %s", MODERATION_STREAM)

    # Ensure moderation stream exists
    client.add_subscriptions(streams=[{"name": MODERATION_STREAM}])

    # Subscribe to all streams so message events are received from every channel.
    # Zulip only delivers message events for streams the bot is subscribed to.
    result = client.get_streams()
    if result["result"] == "success":
        all_streams = [{"name": s["name"]} for s in result["streams"]]
        client.add_subscriptions(streams=all_streams)
        log.info("Subscribed to %d streams", len(all_streams))
    else:
        log.warning("Could not fetch streams: %s", result)

    log.info("Listening for events...")
    client.call_on_each_event(
        handle_event,
        event_types=["message"],
    )


if __name__ == "__main__":
    main()
