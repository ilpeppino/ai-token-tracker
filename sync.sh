#!/usr/bin/env bash
set -euo pipefail

BASE="$(cd "$(dirname "$0")" && pwd)"

"$BASE/.venv/bin/python" "$BASE/sync-usage.py"
