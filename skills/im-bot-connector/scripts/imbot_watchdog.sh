#!/usr/bin/env bash
# im-bot connector watchdog — per-profile PID file, no pgrep, no flock.
# Called by cron every 2 minutes. Ensures exactly one listener per profile.
#
# Usage: imbot_watchdog.sh [profile-name]
#   profile-name defaults to "default", used to namespace PID files.
#
# DESIGN:
#   - Each profile gets its own PID file: /tmp/hermes-imbot-<profile>.pid
#   - Kill -0 checks if the PID is still alive (no pgrep false matches)
#   - Cron is only a backup — the listener v6 self-heals via async socket.io
#   - If listener is dead, launch it and write new PID

PROFILE="${1:-default}"
PIDFILE="/tmp/hermes-imbot-${PROFILE}.pid"
LISTENER="${HOME}/.local/bin/hermes-imbot-listener"

# Check if a valid listener is already running for this profile
if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        exit 0  # Alive — nothing to do
    fi
    # Dead PID file — clean it
    rm -f "$PIDFILE"
fi

# No listener running — start one
echo "[$(date -u +%H:%M:%SZ)] Starting ${PROFILE} listener..."

if [ -x "$LISTENER" ]; then
    nohup python3 "$LISTENER" >> /tmp/hermes-imbot-${PROFILE}.log 2>&1 &
    echo $! > "$PIDFILE"
    echo "   PID: $(cat $PIDFILE)"
else
    echo "   ERROR: Listener not found at $LISTENER"
    exit 1
fi
