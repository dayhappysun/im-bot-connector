#!/usr/bin/env bash
# im-bot connector watchdog: ensure the Hermes agent listener is running.
# Restarts it (detached) if it has died. Stays silent when all is well.
# Match the actual interpreter+script (not a bare script name) so casual shell
# commands that merely mention the script don't cause false "already running".
#
# IMBOT_TIMEOUT no longer KILLS a run — it is how often the connector posts a
# progress / "still working" update to the chat (60s = once a minute). Only
# IMBOT_HARD_TIMEOUT=0 means a task is NEVER auto-killed (runs as long as it
# reasonably needs); set >0 only as an outer cap to reap leaked processes.
IMBOT_TIMEOUT=60
IMBOT_HARD_TIMEOUT=0
IMBOT_STALE_AFTER=120

# Kill any stale default processes first
pkill -f "imbot-venv/bin/python.*hermes_imbot_listener\.py" 2>/dev/null
sleep 1

if ! pgrep -f "imbot-venv/bin/python.*hermes_imbot_listener\.py" >/dev/null 2>&1; then
  cd /root/workspace/im-bot/skills/im-bot-connector/scripts || exit 0
  HOME=/root IMBOT_TIMEOUT=$IMBOT_TIMEOUT IMBOT_HARD_TIMEOUT=$IMBOT_HARD_TIMEOUT IMBOT_STALE_AFTER=$IMBOT_STALE_AFTER \
    setsid /root/workspace/imbot-venv/bin/python \
    hermes_imbot_listener.py >> /var/log/hermes-imbot.log 2>&1 < /dev/null &
  echo "im-bot connector listener was down — restarted at $(date -u +%H:%M:%SZ)"
fi
exit 0
