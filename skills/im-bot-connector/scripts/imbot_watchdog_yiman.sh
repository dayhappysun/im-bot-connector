#!/usr/bin/env bash
# yiman connector watchdog — PID file approach
PROFILE="yiman"
PIDFILE="/tmp/hermes-imbot-${PROFILE}.pid"
LAUNCHER="/root/.hermes/profiles/yiman/skills/im-bot-connector/templates/launch-yiman-listener.sh"

if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE")
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        exit 0
    fi
    rm -f "$PIDFILE"
fi

echo "[$(date -u +%H:%M:%SZ)] Starting ${PROFILE} listener..."
IMBOT_STALE_AFTER=120 "$LAUNCHER" >> /root/.hermes/profiles/yiman/connector.log 2>&1 &
echo $! > "$PIDFILE"
