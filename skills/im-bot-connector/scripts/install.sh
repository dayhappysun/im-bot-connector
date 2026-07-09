#!/usr/bin/env bash
# ============================================================
# im-bot Agent Installation Script (v6 unified)
# Connects your Hermes / OpenClaw / Claude agent to im-bot.
#
# Usage:
#   ./install.sh --invite-code <CODE> [--server <URL>] [--model <MODEL>]
#                [--backend hermes|openclaw|claude|auto]
#
# Example:
#   ./install.sh --invite-code YOUR_INVITE_CODE
#   ./install.sh --invite-code YOUR_CODE --backend openclaw --model deepseek-chat
#
# What it does:
#   1. Detects backend (hermes/openclaw/claude) or uses --backend
#   2. Installs aiohttp for async Socket.io (if missing)
#   3. Saves credentials to ~/.hermes/imbot_agent.json
#   4. Copies listener script to ~/.local/bin/hermes-imbot-listener
#   5. Installs systemd user service (or crontab @reboot fallback)
#   6. Starts the agent listener
# ============================================================
set -euo pipefail

INVITE_CODE=""
SERVER_URL="https://im-bot.net"
HERMES_MODEL="${HERMES_INFERENCE_MODEL:-}"
IMBOT_BACKEND="${IMBOT_BACKEND:-auto}"
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# ── Parse args ────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --invite-code) INVITE_CODE="$2"; shift 2 ;;
    --server)      SERVER_URL="$2"; shift 2 ;;
    --model)       HERMES_MODEL="$2"; shift 2 ;;
    --backend)     IMBOT_BACKEND="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 --invite-code <CODE> [--server <URL>] [--model <MODEL>] [--backend hermes|openclaw|claude|auto]"
      echo ""
      echo "Options:"
      echo "  --invite-code CODE   Agent invite code from im-bot (required)"
      echo "  --server URL         im-bot server URL (default: https://im-bot.net)"
      echo "  --model MODEL        Model override (backend-specific)"
      echo "  --backend NAME       Agent backend: hermes | openclaw | claude | auto (default: auto)"
      echo ""
      echo "Get your invite code: https://im-bot.net/app/ → Create Agent → copy code"
      exit 0
      ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
done

if [[ -z "$INVITE_CODE" ]]; then
  echo "❌ --invite-code is required. Get it from https://im-bot.net/app/"
  exit 1
fi

echo "🤖 im-bot Agent Installer (v6 — unified backend)"
echo "   Server: $SERVER_URL"
echo "   Invite code: ${INVITE_CODE:0:8}..."

# ── Step 0: Detect backend ────────────────────────────────
if [[ "$IMBOT_BACKEND" == "auto" ]]; then
  if command -v openclaw &> /dev/null; then
    IMBOT_BACKEND="openclaw"
  elif command -v hermes &> /dev/null; then
    IMBOT_BACKEND="hermes"
  elif command -v claude &> /dev/null; then
    IMBOT_BACKEND="claude"
  else
    IMBOT_BACKEND="hermes"  # fallback
  fi
fi
echo "   Backend: $IMBOT_BACKEND"
[[ -n "$HERMES_MODEL" ]] && echo "   Model: $HERMES_MODEL"

# ── Step 0b: Install aiohttp (required for async Socket.io) ─
echo ""
echo "📦 [0/4] Checking aiohttp..."
if ! python3 -c "import aiohttp" 2>/dev/null; then
  echo "   Installing aiohttp..."
  python3 -m pip install --quiet aiohttp 2>&1 || {
    echo "   ⚠️ pip install failed, trying --break-system-packages..."
    python3 -m pip install --quiet --break-system-packages aiohttp 2>&1 || {
      echo "   ❌ Could not install aiohttp. Install manually: pip3 install aiohttp"
      exit 1
    }
  }
fi
echo "   ✅ aiohttp available"

# ── Step 1: Save credentials ──────────────────────────────
echo ""
echo "📝 [1/4] Saving credentials..."
mkdir -p ~/.hermes
cat > ~/.hermes/imbot_agent.json << EOF
{
  "serverUrl": "$SERVER_URL",
  "inviteCode": "$INVITE_CODE",
  "model": "$HERMES_MODEL"
}
EOF
chmod 600 ~/.hermes/imbot_agent.json
echo "   ✅ Saved to ~/.hermes/imbot_agent.json"

# ── Step 2: Install listener script ───────────────────────
echo "📋 [2/4] Installing listener script..."
LISTENER_SRC="$SKILL_DIR/scripts/hermes_imbot_listener.py"
LISTENER_DST="$HOME/.local/bin/hermes-imbot-listener"

if [[ ! -f "$LISTENER_SRC" ]]; then
  echo "   ❌ Listener script not found at $LISTENER_SRC"
  echo "   Run this from the im-bot skill directory."
  exit 1
fi

mkdir -p "$HOME/.local/bin"
cp "$LISTENER_SRC" "$LISTENER_DST"
chmod +x "$LISTENER_DST"
echo "   ✅ Installed to $LISTENER_DST"

# ── Step 3: Install service (systemd or crontab) ──────────
echo "🔧 [3/4] Installing service..."

SERVICE_NAME="hermes-imbot"

if command -v systemctl &> /dev/null && systemctl --user list-units &> /dev/null 2>&1; then
  # systemd user service
  mkdir -p ~/.config/systemd/user

  cat > ~/.config/systemd/user/${SERVICE_NAME}.service << UNIT
[Unit]
Description=im-bot Agent Listener (${IMBOT_BACKEND})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$LISTENER_DST
Restart=always
RestartSec=10
Environment=IMBOT_MODEL=${HERMES_MODEL}
Environment=IMBOT_BACKEND=${IMBOT_BACKEND}
Environment=IMBOT_TIMEOUT=180

[Install]
WantedBy=default.target
UNIT

  systemctl --user daemon-reload
  systemctl --user enable "$SERVICE_NAME"
  echo "   ✅ systemd user service installed ($SERVICE_NAME)"

  # Stop old instance if running
  systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
  # Start fresh
  systemctl --user restart "$SERVICE_NAME" || echo "   ⚠️ Could not start service (try: systemctl --user start $SERVICE_NAME)"

else
  # Fallback: crontab @reboot
  echo "   ⚠️ systemd not available — using crontab @reboot fallback"

  crontab -l 2>/dev/null | grep -v "$LISTENER_DST" > /tmp/hermes_cron_tmp || true
  echo "@reboot sleep 30 && $LISTENER_DST >> /tmp/hermes_imbot.log 2>&1" >> /tmp/hermes_cron_tmp
  crontab /tmp/hermes_cron_tmp
  rm /tmp/hermes_cron_tmp
  echo "   ✅ Added to crontab @reboot"

  # Start now
  nohup "$LISTENER_DST" >> /tmp/hermes_imbot.log 2>&1 &
  echo "   ✅ Started in background (PID $!)"
fi

# ── Step 4: Verify ────────────────────────────────────────
echo "🔍 [4/4] Verifying..."
sleep 3

if command -v systemctl &> /dev/null && systemctl --user list-units &> /dev/null 2>&1; then
  if systemctl --user is-active --quiet "$SERVICE_NAME"; then
    echo "   ✅ Service is active"
    journalctl --user -u "$SERVICE_NAME" -n 3 --no-pager 2>/dev/null || true
  else
    echo "   ⚠️ Service not active yet. Check: journalctl --user -u $SERVICE_NAME"
  fi
elif [[ -f /tmp/hermes_imbot.log ]]; then
  if grep -q "Connected to im-bot" /tmp/hermes_imbot.log 2>/dev/null; then
    echo "   ✅ Agent connected to im-bot!"
  else
    echo "   ⚠️ Agent may still be connecting. Check: tail -f /tmp/hermes_imbot.log"
  fi
fi

echo ""
echo "═══════════════════════════════════════════"
echo "✅ Installation complete!"
echo ""
echo "   Agent listener: $LISTENER_DST"
echo "   Config:        ~/.hermes/imbot_agent.json"
echo "   Backend:       $IMBOT_BACKEND"
echo "   Logs:          journalctl --user -u $SERVICE_NAME -f"
echo ""
echo "   Try chatting with your agent:"
echo "   https://im-bot.net/app/"
echo "═══════════════════════════════════════════"
