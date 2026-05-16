#!/usr/bin/env bash
set -euo pipefail

BASE="$(cd "$(dirname "$0")" && pwd)"

"$BASE/.venv/bin/python" "$BASE/scripts/sync-usage.py"
"$BASE/.venv/bin/streamlit" run "$BASE/scripts/dashboard.py" --server.headless=false
