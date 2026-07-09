#!/usr/bin/env bash
# im-bot connector watchdog — flock-based mutual exclusion.
# Called by cron every 2 minutes. Guarantees at most one listener per profile.
#
# Usage: imbot_watchdog.sh [profile-name]
#
# DESIGN:
#   - flock on per-profile lock fd prevents concurrent cron runs.
#   - Flock is KERNEL-LEVEL: process crash → kernel releases lock. No stale locks.
#   - kill -0 on saved PID checks liveness.
#   - Listener writes its own PID on startup (watchdog only reads it).
#   - Cron is a backup; the listener v6 self-heals via async socket.io.

PROFILE="${1:-default}"
LOCKFILE="/tmp/hermes-imbot-${PROFILE}.lock"
PIDFILE="/tmp/hermes-imbot-${PROFILE}.pid"
LISTENER="${HOME}/.local/bin/hermes-imbot-listener"

exec 9>"$LOCKFILE"
flock -n 9 || exit 0

if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        exit 0  # Listener alive — nothing to do
    fi
    rm -f "$PIDFILE"
fi

echo "[$(date -u +%H:%M:%SZ)] Starting ${PROFILE} listener..."
if [ -x "$LISTENER" ]; then
    nohup python3 "$LISTENER" >> /tmp/hermes-imbot-${PROFILE}.log 2>&1 &
    echo $! > "$PIDFILE"
    echo "   PID: $(cat $PIDFILE)"
else
    echo "   ERROR: Listener not found at $LISTENER"
    exit 1
fi

# Flock auto-released when this script exits (kernel closes fd 9)
