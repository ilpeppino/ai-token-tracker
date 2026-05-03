#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / "usage.sqlite"
ENV_PATH = PROJECT_DIR / ".env"
STATE_PATH = PROJECT_DIR / ".reset-notify-state.json"

PROVIDER_ICONS = {
    "codex": "🤖",
    "claude": "🧠",
}

PROVIDER_LABELS = {
    "codex": "Codex",
    "claude": "Claude",
}


def load_env() -> dict[str, str]:
    values = dict(os.environ)

    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")

    return values


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def fmt_datetime(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        raw = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        return dt.strftime("%d-%m-%Y %H:%M")
    except Exception:
        return str(value)[:16]


def parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        raw = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def provider_label(provider: str) -> str:
    key = provider.lower()
    icon = PROVIDER_ICONS.get(key, "🔔")
    label = PROVIDER_LABELS.get(key, key.capitalize())
    return f"{icon} {label}"


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")

    request = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(request, timeout=20) as response:
        response.read()


def read_reset_rows() -> list[sqlite3.Row]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        return conn.execute(
            """
            SELECT
              provider,
              observed_at,
              exact_five_hour_window_end_at,
              exact_weekly_window_end_at,
              actual_hours_until_5h_reset,
              actual_hours_until_weekly_reset,
              five_hour_reset_status,
              weekly_reset_status
            FROM quota_forecast
            ORDER BY provider
            """
        ).fetchall()
    finally:
        conn.close()


def build_reset_message(provider: str, window_label: str, reset_at: str) -> str:
    label = provider_label(provider)

    return "\n".join(
        [
            f"✅ {label} {window_label} limit has reset",
            "",
            "You can start using it again.",
            f"Reset time: {fmt_datetime(reset_at)}",
            "",
            "AI Token Tracker",
        ]
    )


def main() -> None:
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env")

    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")

    state = load_state()
    now = datetime.now(timezone.utc)

    sent = 0
    pending = 0

    for row in read_reset_rows():
        provider = str(row["provider"])

        checks = [
            (
                "5-hour",
                row["exact_five_hour_window_end_at"],
                row["five_hour_reset_status"],
            ),
            (
                "weekly",
                row["exact_weekly_window_end_at"],
                row["weekly_reset_status"],
            ),
        ]

        for window_label, reset_at, reset_status in checks:
            reset_dt = parse_dt(reset_at)

            if reset_status != "known_reset" or reset_dt is None:
                continue

            key = f"{provider}:{window_label}:{reset_at}"

            if state.get(key):
                continue

            if now >= reset_dt:
                send_telegram(
                    token,
                    chat_id,
                    build_reset_message(provider, window_label, reset_at),
                )
                state[key] = {
                    "sent_at": now.isoformat(),
                    "reset_at": reset_at,
                }
                sent += 1
            else:
                pending += 1

    save_state(state)

    print(f"Reset notifications sent: {sent}")
    print(f"Known future resets pending: {pending}")


if __name__ == "__main__":
    main()
