#!/usr/bin/env bash
# Send one Telegram message from the VM host. Reads creds from .env.compose.
# Usage: bash tg_send.sh "message text"
# Silent no-op when creds are absent — never blocks the caller.
ENVC=/opt/option_trading/.env.compose
TOKEN=$(grep '^ALERT_TELEGRAM_TOKEN=' "$ENVC" 2>/dev/null | head -1 | cut -d= -f2-)
CHAT=$(grep '^ALERT_TELEGRAM_CHAT_ID=' "$ENVC" 2>/dev/null | head -1 | cut -d= -f2-)
[ -n "$TOKEN" ] && [ -n "$CHAT" ] || exit 0
curl -s -o /dev/null --max-time 15 \
  "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  -d chat_id="$CHAT" --data-urlencode text="$1" || true
