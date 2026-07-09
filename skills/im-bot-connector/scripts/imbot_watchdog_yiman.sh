#!/usr/bin/env bash
# yiman connector watchdog — flock-based mutual exclusion.
# The launch script manages its own PID file + stop_listener(); 
# this watchdog only provides the cron trigger + mutual exclusion via flock.

PROFILE="yiman"
LOCKFILE="/tmp/hermes-imbot-${PROFILE}.lock"
LAUNCHER="/root/.hermes/profiles/yiman/skills/im-bot-connector/templates/launch-yiman-listener.sh"

exec 9>"$LOCKFILE"
flock -n 9 || exit 0

echo "[$(date -u +%H:%M:%SZ)] Starting ${PROFILE} listener (via launcher)..."
IMBOT_STALE_AFTER=120 "$LAUNCHER" >> /root/.hermes/profiles/yiman/connector.log 2>&1
