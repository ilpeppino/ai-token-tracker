#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape

PROJECT_DIR = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_DIR / "usage.sqlite"
ENV_PATH = PROJECT_DIR / ".env"
STATE_PATH = PROJECT_DIR / ".telegram-bot-state.json"
LOCAL_TIMEZONE = ZoneInfo("Europe/Amsterdam")
AI_TOKENS_SCRIPT = PROJECT_DIR / "ai-tokens"
STATUS_REFRESH_TIMEOUT_SECONDS = 90


def load_env() -> dict[str, str]:
    values = dict(os.environ)
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def telegram_api(token: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None
    if params:
        data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode("utf-8"))


def send_message(token: str, chat_id: str | int, text: str) -> None:
    telegram_api(token, "sendMessage", {
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"offset": 0}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {"offset": 0}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))



def fmt_datetime(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        raw = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is not None:
            dt = dt.astimezone(LOCAL_TIMEZONE)
        return dt.strftime("%d-%m-%Y %H:%M")
    except Exception:
        return str(value)[:16]


def fmt_pct(v: Any) -> str:
    return "n/a" if v is None else f"{float(v):.0f}%"


def fmt_hours(v: Any) -> str:
    if v is None:
        return "n/a"
    h = float(v)
    return f"{round(h * 60):.0f} min" if h < 1 else f"{h:.1f}h"


def fmt_int(v: Any) -> str:
    try:
        return f"{int(v or 0):,}"
    except Exception:
        return "0"


def risk_label(value: Any) -> str:
    risk = str(value or "unknown").lower()
    if risk == "critical":
        return "🔴 Critical"
    if risk == "warning":
        return "🟠 Warning"
    if risk == "ok":
        return "🟢 OK"
    if risk == "insufficient_data":
        return "⚪ Needs data"
    return escape(str(value or "unknown"))


def metric_table(rows: list[tuple[str, str]]) -> str:
    label_width = max(len(label) for label, _ in rows)
    lines = [f"{label:<{label_width}}  {value}" for label, value in rows]
    return "<pre>" + escape("\n".join(lines)) + "</pre>"


def provider_icon(provider: str) -> str:
    return {"codex": "🤖", "claude": "🧠"}.get(provider.lower(), "🔔")


def refresh_usage_before_status() -> tuple[bool, str]:
    """Best-effort refresh before replying to Telegram status commands.

    This intentionally falls back to the last database state if refresh/sync fails.
    """
    try:
        result = subprocess.run(
            [str(AI_TOKENS_SCRIPT), "sync"],
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            timeout=STATUS_REFRESH_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"Refresh timed out after {STATUS_REFRESH_TIMEOUT_SECONDS}s. Showing last cached data."
    except Exception as exc:
        return False, f"Refresh failed: {escape(str(exc))}. Showing last cached data."

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        if len(detail) > 240:
            detail = detail[:240] + "…"
        return False, f"Refresh failed. Showing last cached data. <code>{escape(detail)}</code>"

    return True, "Refreshed just now."


def freshness_note(success: bool, message: str) -> str:
    if success:
        return f"🟢 <i>{escape(message)}</i>"
    return f"⚠️ <i>{message}</i>"


def get_forecast(provider: str | None = None) -> list[sqlite3.Row]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        where = ""
        args: tuple[Any, ...] = ()
        if provider:
            where = "WHERE lower(provider) = lower(?)"
            args = (provider,)

        return conn.execute(f"""
            SELECT
              provider,
              observed_at,
              five_hour_used_pct,
              five_hour_remaining_pct,
              weekly_used_pct,
              weekly_remaining_pct,
              estimated_hours_to_5h_limit,
              estimated_hours_to_weekly_limit,
              actual_hours_until_5h_reset,
              actual_hours_until_weekly_reset,
              five_hour_reset_status,
              weekly_reset_status,
              five_hour_risk,
              weekly_risk,
              toktok_last_5h,
              toktok_last_7d,
              estimated_5h_capacity_toktok,
              estimated_weekly_capacity_toktok
            FROM quota_forecast
            {where}
            ORDER BY provider
        """, args).fetchall()
    finally:
        conn.close()


def build_status(provider: str | None = None, refresh_note: str | None = None) -> str:
    rows = get_forecast(provider)
    if not rows:
        return "No forecast data found. Run <code>ai-tokens sync</code> first."

    parts = ["📊 <b>AI Token Tracker Status</b>"]
    if refresh_note:
        parts.extend([refresh_note, ""])
    else:
        parts.append("")

    for index, r in enumerate(rows):
        p = str(r["provider"]).capitalize()
        icon = provider_icon(str(r["provider"]))

        if index > 0:
            parts.extend(["━━━━━━━━━━━━━━", ""])

        parts.extend([
            f"{icon} <b>{escape(p)}</b>",
            f"Observed: <b>{fmt_datetime(r['observed_at'])}</b>",
            "",
            "⏱️ <b>5-hour window</b>",
            metric_table([
                ("Used", fmt_pct(r["five_hour_used_pct"])),
                ("Remaining", fmt_pct(r["five_hour_remaining_pct"])),
                ("Limit ETA", fmt_hours(r["estimated_hours_to_5h_limit"])),
                ("Reset", fmt_hours(r["actual_hours_until_5h_reset"])),
                ("Risk", risk_label(r["five_hour_risk"])),
            ]),
            "📅 <b>Weekly window</b>",
            metric_table([
                ("Used", fmt_pct(r["weekly_used_pct"])),
                ("Remaining", fmt_pct(r["weekly_remaining_pct"])),
                ("Limit ETA", fmt_hours(r["estimated_hours_to_weekly_limit"])),
                ("Reset", fmt_hours(r["actual_hours_until_weekly_reset"])),
                ("Risk", risk_label(r["weekly_risk"])),
            ]),
            "🧮 <b>Toktok</b>",
            metric_table([
                ("Last 5h", fmt_int(r["toktok_last_5h"])),
                ("Last 7d", fmt_int(r["toktok_last_7d"])),
            ]),
            "",
        ])

    return "\n".join(parts).strip()


def help_text() -> str:
    return "\n".join([
        "<b>AI Token Tracker commands</b>",
        "",
        "<code>/status</code> - Refresh, then show Codex + Claude status",
        "<code>/forecast</code> - Same as status",
        "<code>/codex</code> - Refresh, then show Codex only",
        "<code>/claude</code> - Refresh, then show Claude only",
        "<code>/help</code> - Command list",
    ])


def handle_command(text: str) -> str:
    cmd = text.strip().split()[0].lower()

    if cmd in {"/start", "/help"}:
        return help_text()
    if cmd in {"/status", "/forecast"}:
        ok, note = refresh_usage_before_status()
        return build_status(refresh_note=freshness_note(ok, note))
    if cmd == "/codex":
        ok, note = refresh_usage_before_status()
        return build_status("codex", refresh_note=freshness_note(ok, note))
    if cmd == "/claude":
        ok, note = refresh_usage_before_status()
        return build_status("claude", refresh_note=freshness_note(ok, note))

    return "Unknown command. Send <code>/help</code>."


def main() -> None:
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    allowed_chat_id = env.get("TELEGRAM_CHAT_ID", "")

    if not token or not allowed_chat_id:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env")

    state = load_state()
    offset = int(state.get("offset", 0) or 0)

    print("Telegram bot polling started. Press Ctrl+C to stop.")

    while True:
        updates = telegram_api(token, "getUpdates", {
            "offset": offset,
            "timeout": 25,
        })

        for upd in updates.get("result", []):
            offset = max(offset, int(upd["update_id"]) + 1)
            state["offset"] = offset
            save_state(state)

            msg = upd.get("message") or {}
            chat = msg.get("chat") or {}
            chat_id = chat.get("id")
            text = msg.get("text", "")

            if str(chat_id) != str(allowed_chat_id):
                if chat_id:
                    send_message(token, chat_id, "Unauthorized chat.")
                continue

            if not text:
                continue

            reply = handle_command(text)
            send_message(token, chat_id, reply)

        time.sleep(1)


if __name__ == "__main__":
    main()
