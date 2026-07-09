#!/usr/bin/env bash
# im-bot connector watchdog — flock-based mutual exclusion.
PROFILE="${1:-default}"
LOCKFILE="/tmp/hermes-imbot-${PROFILE}.lock"
PIDFILE="/tmp/hermes-imbot-${PROFILE}.pid"
LISTENER="/root/workspace/im-bot/skills/im-bot-connector/scripts/hermes_imbot_listener.py"
PYTHON="/root/workspace/imbot-venv/bin/python3"

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
"$PYTHON" "$LISTENER" >> "/tmp/hermes-imbot-${PROFILE}.log" 2>&1 &
echo $! > "$PIDFILE"
echo "   PID: $(cat $PIDFILE)"
