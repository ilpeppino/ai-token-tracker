#!/usr/bin/env bash
set -euo pipefail

BASE="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_FILE="$UNIT_DIR/ai-token-tracker-telegram.service"

mkdir -p "$UNIT_DIR"

cat >"$UNIT_FILE" <<EOF
[Unit]
Description=AI Token Tracker Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$BASE
ExecStart=$BASE/.venv/bin/python $BASE/scripts/telegram.py service
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=AI_TOKENS_TELEGRAM_REFRESH_COMMAND=
Environment=AI_TOKENS_TELEGRAM_SYNC_COMMAND=
EnvironmentFile=-$BASE/.env

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now ai-token-tracker-telegram.service

echo "Installed and started ai-token-tracker Telegram service."
echo "Logs:"
echo "  journalctl --user -u ai-token-tracker-telegram.service -f"
echo "To keep it running after logout or reboot, enable lingering:"
echo "  sudo loginctl enable-linger $(id -un)"
