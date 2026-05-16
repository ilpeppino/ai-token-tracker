# Project Structure

This repository keeps stable user-facing commands in the project root and moves implementation details into `scripts/`.

## Root

- `ai-tokens`: main command dispatcher installed into `~/.local/bin`.
- `install.sh`: creates `.venv`, installs dependencies, and refreshes the `ai-tokens` symlink.
- `run.sh`, `sync.sh`, `export.sh`: compatibility wrappers for common local workflows.
- `requirements.txt`: Python runtime dependencies.
- `README.md`: user-facing setup and workflow notes.

## Scripts

- `scripts/sync-usage.py`: canonical local Claude/Codex ingestion into `usage.sqlite`.
- `scripts/sync-usage-percentages.py`: browser dump ingestion into quota percentage snapshots.
- `scripts/build-calibration-view.py`: builds the calibration view.
- `scripts/build-quota-forecast-view.py`: builds the quota forecast view.
- `scripts/dashboard.py`: Streamlit dashboard.
- `scripts/notify.py`, `scripts/reset-notify.py`, `scripts/telegram-bot.py`: Telegram notification entry points.
- `scripts/read-open-usage-tabs.sh`, `scripts/refresh-usage-tabs.sh`: macOS Chrome automation helpers.
- `scripts/export-usage.py`: CSV/JSON export.
- `scripts/token-stats.sh`: terminal stats helper.
- `scripts/analyze-claude-weights.py`, `scripts/parse-usage-dumps.py`, `scripts/usage-page-scrape.py`: analysis and scraping utilities.

## Generated Data

- `usage.sqlite`: local SQLite database.
- `usage-dumps/`: browser page text captured from open authenticated tabs.
- `exports/`: CSV/JSON backups.
- `browser-profile/`: Playwright browser profile for the optional scraper.
- `.notify-state.json`, `.reset-notify-state.json`, `.telegram-bot-state.json`: local notification state.
