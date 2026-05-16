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

NOTIFY_STATE_PATH = PROJECT_DIR / ".notify-state.json"
RESET_STATE_PATH = PROJECT_DIR / ".reset-notify-state.json"
BOT_STATE_PATH = PROJECT_DIR / ".telegram-bot-state.json"

STATUS_REFRESH_TIMEOUT_SECONDS = 90

DEFAULT_THRESHOLDS = {
    "five_hour_warning_pct": 70,
    "five_hour_high_pct": 85,
    "five_hour_critical_pct": 95,
    "weekly_warning_pct": 70,
    "weekly_high_pct": 85,
    "weekly_critical_pct": 95,
    "five_hour_limit_eta_hours": 0.5,
    "weekly_limit_eta_hours": 12,
    "dedupe_seconds": 0,
}

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


def fmt_change(old_value: Any, new_value: Any) -> str:
    if old_value is None:
        return f"new {fmt_pct(new_value)}"
    try:
        old_f = float(old_value)
        new_f = float(new_value)
        diff = new_f - old_f
        sign = "+" if diff > 0 else ""
        return f"{fmt_pct(old_f)} → {fmt_pct(new_f)} ({sign}{diff:.0f} pp)"
    except Exception:
        return f"{old_value} → {new_value}"


def provider_icon(provider: str) -> str:
    return PROVIDER_ICONS.get(provider.lower(), "🔔")


def provider_label(provider: str) -> str:
    key = provider.lower()
    return f"{provider_icon(key)} {PROVIDER_LABELS.get(key, key.capitalize())}"


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
            FROM quota_forecast
            {where}
            ORDER BY provider
            """,
            args,
        ).fetchall()
    finally:
        conn.close()


def build_usage_message(row: sqlite3.Row, headline: str) -> str:
    return "\n".join(
        [
            headline,
            "",
            f"Provider: {provider_label(str(row['provider']))}",
            "AI Token Tracker",
            f"Observed: {fmt_datetime(row['observed_at'])}",
            "",
            "⏱️ 5-hour window:",
            f"- used: {fmt_pct(row['five_hour_used_pct'])}",
            f"- remaining: {fmt_pct(row['five_hour_remaining_pct'])}",
            f"- estimated limit ETA: {fmt_hours(row['estimated_hours_to_5h_limit'])}",
            f"- reset countdown: {fmt_hours(row['actual_hours_until_5h_reset'])}",
            f"- reset status: {row['five_hour_reset_status']}",
            f"- risk: {row['five_hour_risk']}",
            "",
            "📅 Weekly window:",
            f"- used: {fmt_pct(row['weekly_used_pct'])}",
            f"- remaining: {fmt_pct(row['weekly_remaining_pct'])}",
            f"- estimated limit ETA: {fmt_hours(row['estimated_hours_to_weekly_limit'])}",
            f"- reset countdown: {fmt_hours(row['actual_hours_until_weekly_reset'])}",
            f"- reset status: {row['weekly_reset_status']}",
            f"- risk: {row['weekly_risk']}",
            "",
            "🧮 Toktok:",
            f"- last 5h: {fmt_int(row['toktok_last_5h'])}",
            f"- last 7d/window: {fmt_int(row['toktok_last_7d'])}",
            f"- estimated 5h capacity: {fmt_int(row['estimated_5h_capacity_toktok'])}",
            f"- estimated weekly capacity: {fmt_int(row['estimated_weekly_capacity_toktok'])}",
        ]
    )


def build_change_message(
    row: sqlite3.Row,
    changed_fields: list[tuple[str, Any, Any]],
) -> str:
    lines = [
        f"🔄 {provider_label(str(row['provider']))} usage changed",
        "",
        "AI Token Tracker",
        f"Observed: {fmt_datetime(row['observed_at'])}",
        "",
        "Changes:",
    ]

    for label, old_value, new_value in changed_fields:
        lines.append(f"- {label}: {fmt_change(old_value, new_value)}")

    return "\n".join(lines) + "\n\n" + build_usage_message(row, "").lstrip()


def normalize_pct(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except Exception:
        return None


def pct_state_key(provider: str) -> str:
    return f"usage_pct_state:{provider}"


def get_changed_percentage_fields(
    row: sqlite3.Row,
    state: dict[str, Any],
) -> list[tuple[str, Any, Any]]:
    provider = str(row["provider"]).lower()
    previous = state.get(pct_state_key(provider), {})
    if not isinstance(previous, dict):
        previous = {}

    changed: list[tuple[str, Any, Any]] = []
    for field, label in {
        "five_hour_used_pct": "5-hour used",
        "weekly_used_pct": "Weekly used",
    }.items():
        current_value = normalize_pct(row[field])
        previous_value = normalize_pct(previous.get(field))
        if current_value is None:
            continue
        if previous_value is None or current_value != previous_value:
            changed.append((label, previous_value, current_value))
    return changed


def update_percentage_state(row: sqlite3.Row, state: dict[str, Any]) -> None:
    provider = str(row["provider"]).lower()
    state[pct_state_key(provider)] = {
        "observed_at": row["observed_at"],
        "five_hour_used_pct": normalize_pct(row["five_hour_used_pct"]),
        "weekly_used_pct": normalize_pct(row["weekly_used_pct"]),
    }


def run_notify() -> None:
    token, chat_id = telegram_credentials()
    state = load_state(NOTIFY_STATE_PATH)
    rows = read_forecast_rows()

    sent = 0
    suppressed = 0

    for row in rows:
        changed_fields = get_changed_percentage_fields(row, state)
        update_percentage_state(row, state)

        if not changed_fields:
            suppressed += 1
            continue

        send_message(token, chat_id, build_change_message(row, changed_fields))
        sent += 1

    save_state(NOTIFY_STATE_PATH, state)

    print(f"Telegram notifications sent: {sent}")
    print(f"Suppressed duplicates:       {suppressed}")


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

    for index, row in enumerate(rows):
        provider_name = str(row["provider"])
        if index > 0:
            parts.extend(["━━━━━━━━━━━━━━", ""])

        parts.extend(
            [
                f"{provider_icon(provider_name)} <b>{escape(provider_name.capitalize())}</b>",
                f"Observed: <b>{fmt_datetime(row['observed_at'])}</b>",
                "",
                "⏱️ <b>5-hour window</b>",
                metric_table(
                    [
                        ("Used", fmt_pct(row["five_hour_used_pct"])),
                        ("Remaining", fmt_pct(row["five_hour_remaining_pct"])),
                        ("Limit ETA", fmt_hours(row["estimated_hours_to_5h_limit"])),
                        ("Reset", fmt_hours(row["actual_hours_until_5h_reset"])),
                        ("Risk", risk_label(row["five_hour_risk"])),
                    ]
                ),
                "📅 <b>Weekly window</b>",
                metric_table(
                    [
                        ("Used", fmt_pct(row["weekly_used_pct"])),
                        ("Remaining", fmt_pct(row["weekly_remaining_pct"])),
                        ("Limit ETA", fmt_hours(row["estimated_hours_to_weekly_limit"])),
                        ("Reset", fmt_hours(row["actual_hours_until_weekly_reset"])),
                        ("Risk", risk_label(row["weekly_risk"])),
                    ]
                ),
                "🧮 <b>Toktok</b>",
                metric_table(
                    [
                        ("Last 5h", fmt_int(row["toktok_last_5h"])),
                        ("Last 7d", fmt_int(row["toktok_last_7d"])),
                    ]
                ),
                "",
            ]
        )

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
            "  notify        Send usage percentage change notifications once.",
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
