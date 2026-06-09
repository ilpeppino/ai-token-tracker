#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
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
NOTIFY_STATE_PATH = PROJECT_DIR / ".notify-state.json"
BOT_STATE_PATH = PROJECT_DIR / ".telegram-bot-state.json"

STATUS_REFRESH_TIMEOUT_SECONDS = 90
POLL_INTERVAL_SECONDS = 5 * 60
USAGE_WARNING_THRESHOLD_PCT = 90.0
TELEGRAM_LONG_POLL_TIMEOUT_SECONDS = 25
TELEGRAM_HTTP_TIMEOUT_BUFFER_SECONDS = 10

PROVIDER_ICONS = {
    "codex": "🤖",
    "claude": "🧠",
}

PROVIDER_LABELS = {
    "codex": "Codex",
    "claude": "Claude",
}

USAGE_NOTIFY_LABELS = {
    "codex": "ChatGPT",
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


def usage_notify_label(provider: str) -> str:
    key = provider.lower()
    return f"{provider_icon(key)} {USAGE_NOTIFY_LABELS.get(key, PROVIDER_LABELS.get(key, key.capitalize()))}"


def clamp_percentage(value: Any) -> float | None:
    try:
        return max(0.0, min(100.0, float(value)))
    except Exception:
        return None


def quota_status_dot(remaining_pct: Any) -> str:
    pct = clamp_percentage(remaining_pct)
    if pct is None:
        return "⚪"
    if pct >= 70:
        return "🟢"
    if pct >= 30:
        return "🟡"
    if pct >= 10:
        return "🟠"
    return "🔴"


def quota_dot_bar(remaining_pct: Any, width: int = 10) -> str:
    pct = clamp_percentage(remaining_pct)
    if pct is None:
        return "⚪" * width

    if pct >= 100:
        filled = width
    elif pct <= 0:
        filled = 0
    else:
        filled = max(1, min(width - 1, int(pct // 10)))

    return quota_status_dot(pct) * filled + "⚪" * (width - filled)


def usage_status_dot(used_pct: Any) -> str:
    pct = clamp_percentage(used_pct)
    if pct is None:
        return "⚪"
    if pct >= USAGE_WARNING_THRESHOLD_PCT:
        return "🔴"
    if pct >= 70:
        return "🟠"
    if pct >= 30:
        return "🟡"
    return "🟢"


def usage_dot_bar(used_pct: Any, width: int = 10) -> str:
    pct = clamp_percentage(used_pct)
    if pct is None:
        return "⚪" * width

    if pct >= 100:
        filled = width
    elif pct <= 0:
        filled = 0
    else:
        filled = max(1, min(width - 1, int(pct // 10)))

    return usage_status_dot(pct) * filled + "⚪" * (width - filled)


def compact_relative_time(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        hours = max(0.0, float(value))
    except Exception:
        return "n/a"

    if hours < 1:
        return f"{max(1, round(hours * 60))}m"
    if hours < 72:
        return f"{round(hours)}h"
    return f"{round(hours / 24)}d"


def compact_reset_at(value: Any) -> str:
    dt = parse_dt(value)
    if dt is None:
        return "n/a"

    local_dt = dt.astimezone(LOCAL_TIMEZONE)
    now = datetime.now(timezone.utc).astimezone(LOCAL_TIMEZONE)

    if local_dt.date() == now.date():
        return local_dt.strftime("%H:%M")
    return local_dt.strftime("%a %H:%M")


def reset_parts(row: sqlite3.Row, window: str) -> tuple[str, str]:
    if window == "five_hour":
        exact = row["exact_five_hour_window_end_at"]
        hours = row["actual_hours_until_5h_reset"]
    else:
        exact = row["exact_weekly_window_end_at"]
        hours = row["actual_hours_until_weekly_reset"]

    return compact_reset_at(exact), compact_relative_time(hours)


def status_line(row: sqlite3.Row, window: str) -> str:
    provider_name = str(row["provider"])
    if window == "five_hour":
        used = row["five_hour_used_pct"]
    else:
        used = row["weekly_used_pct"]

    pct = clamp_percentage(used)
    pct_text = "n/a" if pct is None else f"{pct:.0f}%"
    reset_at, reset_in = reset_parts(row, window)
    provider = USAGE_NOTIFY_LABELS.get(provider_name.lower(), provider_name.capitalize())
    line = f"{provider:<7} {usage_dot_bar(used, width=8)} {pct_text:>4}"
    if reset_at != "n/a":
        line += f" · ↺ {reset_at}"
    return line


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


def usage_detail_lines(details: list[tuple[str, str]]) -> list[str]:
    label_width = max(len(label) for label, _ in details)
    return [f"{label + ':':<{label_width + 1}} {value}" for label, value in details]


def build_usage_change_message(provider: str, used_pct: float, reset_at: Any) -> str:
    return "\n".join(
        [
            f"{usage_notify_label(provider)} 5-hour usage changed",
            "",
            *usage_detail_lines(
                [
                    ("Used", f"{used_pct:.0f}%"),
                    ("Next reset", fmt_datetime(reset_at)),
                ]
            ),
        ]
    )


def build_usage_warning_message(provider: str, used_pct: float, reset_at: Any) -> str:
    return "\n".join(
        [
            f"⚠️ {usage_notify_label(provider)} usage is depleted",
            "",
            *usage_detail_lines(
                [
                    ("Used", f"{used_pct:.0f}%"),
                    ("Next reset", fmt_datetime(reset_at)),
                ]
            ),
        ]
    )


def build_usage_available_message(provider: str, reset_at: Any) -> str:
    banner = "🟩🟩🟩 AVAILABLE 🟩🟩🟩"
    return "\n".join(
        [
            banner,
            f"✅ {provider_label(provider)} is available again",
            banner,
            "",
            "5-hour usage has reset to 0%.",
            *usage_detail_lines(
                [
                    ("Reset time", fmt_datetime(reset_at)),
                ]
            ),
        ]
    )


def notify_for_usage_row(
    token: str,
    chat_id: str,
    state: dict[str, Any],
    row: sqlite3.Row,
) -> bool:
    provider = str(row["provider"]).lower()
    if provider not in {"codex", "claude"}:
        return False

    used_pct = clamp_percentage(row["five_hour_used_pct"])
    if used_pct is None:
        return False

    rounded_used = round(used_pct)
    reset_at = row["exact_five_hour_window_end_at"]
    provider_state = state.setdefault(provider, {})
    previous_used = provider_state.get("five_hour_used_pct")
    previous_warning_sent = bool(provider_state.get("five_hour_warning_sent"))

    message: str | None = None

    if rounded_used == 0:
        if previous_used is not None and float(previous_used) > 0:
            message = build_usage_available_message(provider, reset_at)
        provider_state["five_hour_warning_sent"] = False
    elif rounded_used >= USAGE_WARNING_THRESHOLD_PCT and not previous_warning_sent:
        message = build_usage_warning_message(provider, float(rounded_used), reset_at)
        provider_state["five_hour_warning_sent"] = True
    elif previous_used is not None and rounded_used != round(float(previous_used)):
        message = build_usage_change_message(provider, float(rounded_used), reset_at)
        if rounded_used < USAGE_WARNING_THRESHOLD_PCT:
            provider_state["five_hour_warning_sent"] = False
    elif previous_used is None:
        message = build_usage_change_message(provider, float(rounded_used), reset_at)
        provider_state["five_hour_warning_sent"] = rounded_used >= USAGE_WARNING_THRESHOLD_PCT

    provider_state["five_hour_used_pct"] = float(rounded_used)
    provider_state["five_hour_reset_at"] = reset_at
    provider_state["observed_at"] = row["observed_at"]

    if message is None:
        return False

    send_message(token, chat_id, message)
    provider_state["last_sent_at"] = datetime.now(timezone.utc).isoformat()
    return True


def run_notify() -> None:
    token, chat_id = telegram_credentials()
    state = load_state(NOTIFY_STATE_PATH)

    sent = 0
    for row in read_forecast_rows():
        if notify_for_usage_row(token, chat_id, state, row):
            sent += 1

    save_state(NOTIFY_STATE_PATH, state)
    print(f"Usage notifications sent: {sent}")


def build_reset_message(provider: str, window_label: str, reset_at: str) -> str:
    return "\n".join(
        [
            f"✅ {provider_label(provider)} {window_label} limit has reset",
            "",
            "You can start using it again.",
            f"Reset time: {fmt_datetime(reset_at)}",
            "",
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


def run_usage_sync() -> tuple[bool, str]:
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
        return False, f"sync timed out after {STATUS_REFRESH_TIMEOUT_SECONDS}s"
    except Exception as exc:
        return False, f"sync failed: {exc}"

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        return False, detail[:500]

    return True, "sync complete"


def run_poll_notify(*, include_reset_notify: bool = False) -> None:
    print("Telegram usage polling started. Press Ctrl+C to stop.", flush=True)

    while True:
        started = time.monotonic()
        ok, detail = run_usage_sync()
        if not ok:
            print(f"Usage sync failed before notify: {detail}", flush=True)
        else:
            run_notify()
            if include_reset_notify:
                run_reset_notify()

        elapsed = time.monotonic() - started
        time.sleep(max(1.0, POLL_INTERVAL_SECONDS - elapsed))


def run_telegram() -> None:
    poll_thread = threading.Thread(
        target=run_poll_notify,
        kwargs={"include_reset_notify": True},
        daemon=True,
    )
    poll_thread.start()
    run_bot()


def freshness_note(success: bool, message: str) -> str:
    return ""


def build_status(provider: str | None = None, refresh_note: str | None = None) -> str:
    rows = read_forecast_rows(provider)
    if not rows:
        return "No forecast data found. Run <code>ai-tokens sync</code> first."

    latest_observed = max(
        str(row["observed_at"]) for row in rows if row["observed_at"] is not None
    )
    observed_dt = parse_dt(latest_observed)
    observed_time = (
        observed_dt.astimezone(LOCAL_TIMEZONE).strftime("%H:%M")
        if observed_dt is not None
        else "--:--"
    )

    lines = [f"🕒 {observed_time}", "", "⚡ 5h"]
    for row in rows:
        lines.append(status_line(row, "five_hour"))

    lines.extend(["", "📅 Weekly"])
    for row in rows:
        lines.append(status_line(row, "weekly"))

    return "<pre>" + escape("\n".join(lines).strip()) + "</pre>"


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
        try:
            updates = telegram_api(
                token,
                "getUpdates",
                {"offset": offset, "timeout": TELEGRAM_LONG_POLL_TIMEOUT_SECONDS},
                timeout=(
                    TELEGRAM_LONG_POLL_TIMEOUT_SECONDS
                    + TELEGRAM_HTTP_TIMEOUT_BUFFER_SECONDS
                ),
            )
        except (TimeoutError, urllib.error.URLError) as exc:
            print(f"Telegram getUpdates failed, retrying: {exc}", flush=True)
            time.sleep(5)
            continue

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
            "Usage: scripts/telegram.py [notify|poll-notify|reset-notify|bot|telegram]",
            "",
            "Commands:",
            "  notify        Send usage-change notifications once.",
            "  poll-notify   Sync and check usage-change notifications every 5 minutes.",
            "  reset-notify  Send reset notifications once.",
            "  bot           Run the interactive Telegram bot.",
            "  telegram      Run the interactive bot and usage polling together.",
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
    if command == "poll-notify":
        run_poll_notify()
        return
    if command == "reset-notify":
        run_reset_notify()
        return
    if command == "bot":
        run_bot()
        return
    if command == "telegram":
        run_telegram()
        return

    raise SystemExit(usage())


if __name__ == "__main__":
    main()
