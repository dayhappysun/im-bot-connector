#!/usr/bin/env bash
# yiman connector watchdog: ensure the yiman Hermes agent listener is running.
IMBOT_TIMEOUT=60
IMBOT_HARD_TIMEOUT=0
IMBOT_STALE_AFTER=120

if ! pgrep -f "hermes-imbot-listener" > /dev/null 2>&1; then
  IMBOT_STALE_AFTER=$IMBOT_STALE_AFTER \
    /root/.hermes/profiles/yiman/skills/im-bot-connector/templates/launch-yiman-listener.sh \
    >> /root/.hermes/profiles/yiman/connector.log 2>&1
  echo "yiman connector was down — restarted at $(date -u +%H:%M:%SZ)"
fi
exit 0
