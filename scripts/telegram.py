#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROJECT_DIR = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_DIR / "usage.sqlite"
ENV_PATH = PROJECT_DIR / ".env"
AI_TOKENS_SCRIPT = PROJECT_DIR / "ai-tokens"
LOCAL_TIMEZONE = ZoneInfo("Europe/Amsterdam")

RESET_STATE_PATH = PROJECT_DIR / ".reset-notify-state.json"
BOT_STATE_PATH = PROJECT_DIR / ".telegram-bot-state.json"

STATUS_REFRESH_TIMEOUT_SECONDS = 90

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


def telegram_credentials() -> tuple[str, str]:
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env")
    return token, chat_id


def telegram_api(
    token: str,
    method: str,
    params: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode("utf-8") if params else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
        if response.status >= 300:
            raise RuntimeError(payload)
        return json.loads(payload)


def send_message(
    token: str,
    chat_id: str | int,
    text: str,
    *,
    parse_mode: str | None = None,
) -> None:
    params = {
        "chat_id": str(chat_id),
        "text": text,
        "disable_web_page_preview": "true",
    }
    if parse_mode:
        params["parse_mode"] = parse_mode
    telegram_api(token, "sendMessage", params, timeout=20)


def load_state(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return dict(default or {})
    try:
        return json.loads(path.read_text())
    except Exception:
        return dict(default or {})


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


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


def fmt_pct(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.0f}%"
    except Exception:
        return "n/a"


def fmt_hours(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        hours = float(value)
    except Exception:
        return "n/a"
    return f"{round(hours * 60):.0f} min" if hours < 1 else f"{hours:.1f}h"


def fmt_int(value: Any) -> str:
    try:
        return f"{int(value or 0):,}"
    except Exception:
        return "0"


def provider_icon(provider: str) -> str:
    return PROVIDER_ICONS.get(provider.lower(), "🔔")


def provider_label(provider: str) -> str:
    key = provider.lower()
    return f"{provider_icon(key)} {PROVIDER_LABELS.get(key, key.capitalize())}"


def metric_table(rows: list[tuple[str, str]]) -> str:
    label_width = max(len(label) for label, _ in rows)
    lines = [f"{label:<{label_width}}  {value}" for label, value in rows]
    return "<pre>" + escape("\n".join(lines)) + "</pre>"


def progress_bar(value: Any, used_pct: Any, width: int = 12) -> str:
    try:
        pct = max(0.0, min(100.0, float(value)))
        used = max(0.0, min(100.0, float(used_pct)))
    except Exception:
        return "n/a"

    filled = round((pct / 100.0) * width)
    empty = width - filled

    if used >= 90:
        fill = "🟥"
    elif used >= 80:
        fill = "🟨"
    else:
        fill = "🟩"

    return f"{fill * filled}{'⬜' * empty} {pct:.0f}%"


def reset_text(row: sqlite3.Row, window: str) -> str:
    if window == "five_hour":
        exact = row["exact_five_hour_window_end_at"]
        hours = row["actual_hours_until_5h_reset"]
    else:
        exact = row["exact_weekly_window_end_at"]
        hours = row["actual_hours_until_weekly_reset"]

    if exact is None:
        return "n/a"

    suffix = fmt_hours(hours)
    if suffix == "n/a":
        return fmt_datetime(exact)
    return f"{fmt_datetime(exact)} ({suffix})"


def window_block(row: sqlite3.Row, window: str) -> list[str]:
    provider_name = str(row["provider"])
    if window == "five_hour":
        used = row["five_hour_used_pct"]
        remaining = row["five_hour_remaining_pct"]
        eta = row["estimated_hours_to_5h_limit"]
    else:
        used = row["weekly_used_pct"]
        remaining = row["weekly_remaining_pct"]
        eta = row["estimated_hours_to_weekly_limit"]

    return [
        f"{provider_icon(provider_name)} <b>{escape(provider_name.capitalize())}</b>",
        "Used",
        progress_bar(used, used),
        "Remaining",
        progress_bar(remaining, used),
        f"Limit ETA: <b>{escape(fmt_hours(eta))}</b>",
        f"Reset: <b>{escape(reset_text(row, window))}</b>",
        "",
    ]


def read_forecast_rows(provider: str | None = None) -> list[sqlite3.Row]:
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        where = ""
        args: tuple[Any, ...] = ()
        if provider:
            where = "WHERE lower(provider) = lower(?)"
            args = (provider,)

        return conn.execute(
            f"""
            WITH ranked AS (
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
                estimated_5h_capacity_toktok,
                estimated_weekly_capacity_toktok,
                toktok_last_5h,
                toktok_last_7d,
                exact_five_hour_window_end_at,
                exact_weekly_window_end_at,
                ROW_NUMBER() OVER (
                  PARTITION BY lower(provider)
                  ORDER BY
                    CASE
                      WHEN COALESCE(five_hour_used_pct, 0) > 0
                        OR COALESCE(weekly_used_pct, 0) > 0
                        OR exact_five_hour_window_end_at IS NOT NULL
                        OR exact_weekly_window_end_at IS NOT NULL
                      THEN 1
                      ELSE 0
                    END DESC,
                    observed_at DESC
                ) AS row_number
              FROM quota_forecast
              {where}
            )
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
              estimated_5h_capacity_toktok,
              estimated_weekly_capacity_toktok,
              toktok_last_5h,
              toktok_last_7d,
              exact_five_hour_window_end_at,
              exact_weekly_window_end_at
            FROM ranked
            WHERE row_number = 1
            ORDER BY provider
            """,
            args,
        ).fetchall()
    finally:
        conn.close()


def run_notify() -> None:
    print("Usage-change Telegram notifications are disabled.")


def build_reset_message(provider: str, window_label: str, reset_at: str) -> str:
    return "\n".join(
        [
            f"✅ {provider_label(provider)} {window_label} limit has reset",
            "",
            "You can start using it again.",
            f"Reset time: {fmt_datetime(reset_at)}",
            "",
            "AI Token Tracker",
        ]
    )


def run_reset_notify() -> None:
    token, chat_id = telegram_credentials()
    state = load_state(RESET_STATE_PATH)
    now = datetime.now(timezone.utc)

    sent = 0
    pending = 0

    for row in read_forecast_rows():
        provider = str(row["provider"])
        checks = [
            ("5-hour", row["exact_five_hour_window_end_at"], row["five_hour_reset_status"]),
            ("weekly", row["exact_weekly_window_end_at"], row["weekly_reset_status"]),
        ]

        for window_label, reset_at, reset_status in checks:
            reset_dt = parse_dt(reset_at)
            if reset_status != "known_reset" or reset_dt is None:
                continue

            key = f"{provider}:{window_label}:{reset_at}"
            if state.get(key):
                continue

            if now >= reset_dt:
                send_message(token, chat_id, build_reset_message(provider, window_label, reset_at))
                state[key] = {"sent_at": now.isoformat(), "reset_at": reset_at}
                sent += 1
            else:
                pending += 1

    save_state(RESET_STATE_PATH, state)

    print(f"Reset notifications sent: {sent}")
    print(f"Known future resets pending: {pending}")


def refresh_usage_before_status() -> tuple[bool, str]:
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


def build_status(provider: str | None = None, refresh_note: str | None = None) -> str:
    rows = read_forecast_rows(provider)
    if not rows:
        return "No forecast data found. Run <code>ai-tokens sync</code> first."

    parts = ["📊 <b>AI Token Tracker Status</b>"]
    parts.extend([refresh_note, ""] if refresh_note else [""])

    latest_observed = max(str(row["observed_at"]) for row in rows if row["observed_at"] is not None)
    parts.extend([f"Observed: <b>{fmt_datetime(latest_observed)}</b>", ""])

    parts.append("⏱️ <b>5-hour window</b>")
    for row in rows:
        parts.extend(window_block(row, "five_hour"))

    parts.extend(["📅 <b>Weekly limits</b>"])
    for row in rows:
        parts.extend(window_block(row, "weekly"))

    return "\n".join(parts).strip()


def help_text() -> str:
    return "\n".join(
        [
            "<b>AI Token Tracker commands</b>",
            "",
            "<code>/status</code> - Refresh, then show Codex + Claude status",
            "<code>/forecast</code> - Same as status",
            "<code>/codex</code> - Refresh, then show Codex only",
            "<code>/claude</code> - Refresh, then show Claude only",
            "<code>/help</code> - Command list",
        ]
    )


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


def run_bot() -> None:
    token, allowed_chat_id = telegram_credentials()
    state = load_state(BOT_STATE_PATH, {"offset": 0})
    offset = int(state.get("offset", 0) or 0)

    print("Telegram bot polling started. Press Ctrl+C to stop.")

    while True:
        updates = telegram_api(token, "getUpdates", {"offset": offset, "timeout": 25})

        for update in updates.get("result", []):
            offset = max(offset, int(update["update_id"]) + 1)
            state["offset"] = offset
            save_state(BOT_STATE_PATH, state)

            message = update.get("message") or {}
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            text = message.get("text", "")

            if str(chat_id) != str(allowed_chat_id):
                if chat_id:
                    send_message(token, chat_id, "Unauthorized chat.", parse_mode="HTML")
                continue

            if not text:
                continue

            send_message(token, chat_id, handle_command(text), parse_mode="HTML")

        time.sleep(1)


def usage() -> str:
    return "\n".join(
        [
            "Usage: scripts/telegram.py [notify|reset-notify|bot]",
            "",
            "Commands:",
            "  notify        Disabled compatibility command.",
            "  reset-notify  Send reset notifications once.",
            "  bot           Run the interactive Telegram bot.",
        ]
    )


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "bot"

    if command in {"help", "-h", "--help"}:
        print(usage())
        return
    if command == "notify":
        run_notify()
        return
    if command == "reset-notify":
        run_reset_notify()
        return
    if command == "bot":
        run_bot()
        return

    raise SystemExit(usage())


if __name__ == "__main__":
    main()
