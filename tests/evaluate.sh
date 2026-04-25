#!/bin/bash
# TODO: Move this script to tests/evaluate.sh and split payload.json into
# per-tier fixtures under tests/payloads/ (safe, medium_toxic, high_toxic, self_harm).

# Create test payload
cat <<EOF > payload.json
{
  "message_id": "zulip_msg_8821",
  "token": "secure_webhook_token_abc123",
  "metadata": {
    "stream_id": 45,
    "topic": "General Chat"
  },
  "message": {
    "sender_email": "student_01@university.edu",
    "raw_text": "**HEY!!** Check this out: http://bad-site.com/harm. I want to hurt myself.",
    "cleaned_text": "hey check this out i want to hurt myself"
  }
}
EOF

# Run Apache Bench evaluation
echo "Running performance evaluation..."
ab -n 100 -c 10 -p payload.json -T application/json http://127.0.0.1:8000/moderate