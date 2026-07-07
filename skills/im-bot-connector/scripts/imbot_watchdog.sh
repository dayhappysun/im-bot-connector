#!/usr/bin/env bash
# im-bot connector watchdog: ensure the Hermes agent listener is running.
# Restarts it (detached) if it has died. Stays silent when all is well.
IMBOT_TIMEOUT=60
IMBOT_HARD_TIMEOUT=0
IMBOT_STALE_AFTER=120

if ! pgrep -f "imbot-venv/bin/python.*hermes_imbot_listener\.py" >/dev/null 2>&1; then
  cd /root/workspace/im-bot/skills/im-bot-connector/scripts || exit 0
  HOME=/root IMBOT_TIMEOUT=$IMBOT_TIMEOUT IMBOT_HARD_TIMEOUT=$IMBOT_HARD_TIMEOUT IMBOT_STALE_AFTER=$IMBOT_STALE_AFTER \
    setsid /root/workspace/imbot-venv/bin/python \
    hermes_imbot_listener.py >> /var/log/hermes-imbot.log 2>&1 < /dev/null &
  echo "im-bot connector listener was down — restarted at $(date -u +%H:%M:%SZ)"
fi
exit 0
