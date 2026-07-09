#!/usr/bin/env bash
# yiman connector watchdog — flock + PID file
PROFILE="yiman"
LOCKFILE="/tmp/hermes-imbot-${PROFILE}.lock"
PIDFILE="/tmp/hermes-imbot-${PROFILE}.pid"
LAUNCHER="/root/.hermes/profiles/yiman/skills/im-bot-connector/templates/launch-yiman-listener.sh"

exec 9>"$LOCKFILE"
flock -n 9 || exit 0

if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        exit 0
    fi
    rm -f "$PIDFILE"
fi

echo "[$(date -u +%H:%M:%SZ)] Starting ${PROFILE} listener..."
IMBOT_STALE_AFTER=120 "$LAUNCHER" >> /root/.hermes/profiles/yiman/connector.log 2>&1 &
echo $! > "$PIDFILE"
echo "   PID: $(cat $PIDFILE)"
