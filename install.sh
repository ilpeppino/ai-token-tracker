#!/usr/bin/env bash
set -euo pipefail

BASE="$(cd "$(dirname "$0")" && pwd)"

cd "$BASE"

python3 -m venv .venv
"$BASE/.venv/bin/pip" install --upgrade pip
"$BASE/.venv/bin/pip" install -r requirements.txt

mkdir -p "$HOME/.local/bin"

ln -sf "$BASE/ai-tokens" "$HOME/.local/bin/ai-tokens"
ln -sfn "$BASE" "$HOME/.ai-token-tracker"

echo "Installed AI Token Tracker."
echo "Run:"
echo "  ai-tokens dashboard"
