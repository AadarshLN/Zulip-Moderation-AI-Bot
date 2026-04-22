#!/bin/bash
(crontab -l 2>/dev/null; echo "0 1 * * * /home/cc/Zulip-Moderation-AI-Bot/scripts/retrain_latest.sh >> /home/cc/retrain.log 2>&1") | crontab -
