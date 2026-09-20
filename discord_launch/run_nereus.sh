#!/usr/bin/env bash
set -Eeuo pipefail

export DISCORD_BOT_TOKEN="$(aws ssm get-parameter \
  --region us-east-1 \
  --name /orion/nereus_bot_token \
  --with-decryption \
  --query 'Parameter.Value' \
  --output text)"
test -n "$DISCORD_BOT_TOKEN"
exec /opt/venice-bots/venv/bin/python /opt/venice-bots/app/nereus_bot.py
