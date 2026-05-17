#!/usr/bin/env bash
set -euo pipefail

BASE="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE"

"$BASE/.venv/bin/python" "$BASE/scripts/sync-usage.py"
