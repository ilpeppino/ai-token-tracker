#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from datetime import datetime

PROJECT_DIR = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_DIR / "usage.sqlite"
ENV_PATH = PROJECT_DIR / ".env"
STATE_PATH = PROJECT_DIR / ".notify-state.json"


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


# Provider icons and labels
PROVIDER_ICONS = {
    "codex": "🤖",
    "claude": "🧠",
}

PROVIDER_LABELS = {
    "codex": "Codex",
    "claude": "Claude",
}


def provider_label(row: sqlite3.Row) -> str:
    provider_key = str(row["provider"]).lower()
    icon = PROVIDER_ICONS.get(provider_key, "🔔")
    label = PROVIDER_LABELS.get(provider_key, provider_key.capitalize())
    return f"{icon} {label}"


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

    if hours < 1:
        return f"{round(hours * 60):.0f} min"
    return f"{hours:.1f}h"


# --- New helper: fmt_change ---
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
        payload = response.read().decode("utf-8")
        if response.status >= 300:
            raise RuntimeError(payload)


def read_forecast_rows() -> list[sqlite3.Row]:
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute(
            """
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
              toktok_last_7d
            FROM quota_forecast
            ORDER BY provider
            """
        ).fetchall()
    finally:
        conn.close()

    return rows


def threshold_alerts(row: sqlite3.Row, thresholds: dict[str, float]) -> list[tuple[str, str]]:
    provider = provider_label(row)
    alerts: list[tuple[str, str]] = []

    five_pct = row["five_hour_used_pct"]
    week_pct = row["weekly_used_pct"]
    five_eta = row["estimated_hours_to_5h_limit"]
    week_eta = row["estimated_hours_to_weekly_limit"]
    five_reset = row["actual_hours_until_5h_reset"]
    week_reset = row["actual_hours_until_weekly_reset"]

    if five_pct is not None:
        if five_pct >= thresholds["five_hour_critical_pct"]:
            alerts.append(("critical", f"🚨 {provider} 5-hour usage is critical: {fmt_pct(five_pct)}"))
        elif five_pct >= thresholds["five_hour_high_pct"]:
            alerts.append(("high", f"⚠️ {provider} 5-hour usage is high: {fmt_pct(five_pct)}"))
        elif five_pct >= thresholds["five_hour_warning_pct"]:
            alerts.append(("warning", f"⚠️ {provider} 5-hour usage warning: {fmt_pct(five_pct)}"))

    if week_pct is not None:
        if week_pct >= thresholds["weekly_critical_pct"]:
            alerts.append(("critical", f"🚨 {provider} weekly usage is critical: {fmt_pct(week_pct)}"))
        elif week_pct >= thresholds["weekly_high_pct"]:
            alerts.append(("high", f"⚠️ {provider} weekly usage is high: {fmt_pct(week_pct)}"))
        elif week_pct >= thresholds["weekly_warning_pct"]:
            alerts.append(("warning", f"⚠️ {provider} weekly usage warning: {fmt_pct(week_pct)}"))

    if five_eta is not None and five_eta <= thresholds["five_hour_limit_eta_hours"]:
        alerts.append(("critical", f"🚨 {provider} may hit 5-hour limit in {fmt_hours(five_eta)}"))

    if week_eta is not None and week_eta <= thresholds["weekly_limit_eta_hours"]:
        alerts.append(("critical", f"🚨 {provider} may hit weekly limit in {fmt_hours(week_eta)}"))

    if five_eta is not None and five_reset is not None and five_eta < five_reset:
        alerts.append(
            (
                "critical",
                f"🚨 {provider} 5-hour limit is projected before reset: limit in {fmt_hours(five_eta)}, reset in {fmt_hours(five_reset)}",
            )
        )

    if week_eta is not None and week_reset is not None and week_eta < week_reset:
        alerts.append(
            (
                "critical",
                f"🚨 {provider} weekly limit is projected before reset: limit in {fmt_hours(week_eta)}, reset in {fmt_hours(week_reset)}",
            )
        )

    return alerts


def build_message(row: sqlite3.Row, headline: str) -> str:
    provider = provider_label(row)

    return "\n".join(
        [
            headline,
            "",
            f"Provider: {provider}",
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
            f"- last 5h: {int(row['toktok_last_5h'] or 0):,}",
            f"- last 7d/window: {int(row['toktok_last_7d'] or 0):,}",
            f"- estimated 5h capacity: {int(row['estimated_5h_capacity_toktok'] or 0):,}",
            f"- estimated weekly capacity: {int(row['estimated_weekly_capacity_toktok'] or 0):,}",
        ]
    )


# --- New: build_change_message ---
def build_change_message(row: sqlite3.Row, changed_fields: list[tuple[str, Any, Any]]) -> str:
    provider = provider_label(row)

    lines = [
        f"🔄 {provider} usage changed",
        "",
        "AI Token Tracker",
        f"Observed: {fmt_datetime(row['observed_at'])}",
        "",
        "Changes:",
    ]

    for label, old_value, new_value in changed_fields:
        lines.append(f"- {label}: {fmt_change(old_value, new_value)}")

    lines.extend(
        [
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
            f"- last 5h: {int(row['toktok_last_5h'] or 0):,}",
            f"- last 7d/window: {int(row['toktok_last_7d'] or 0):,}",
            f"- estimated 5h capacity: {int(row['estimated_5h_capacity_toktok'] or 0):,}",
            f"- estimated weekly capacity: {int(row['estimated_weekly_capacity_toktok'] or 0):,}",
        ]
    )

    return "\n".join(lines)


def should_send(state: dict[str, Any], key: str, dedupe_seconds: int) -> bool:
    now = time.time()
    last = float(state.get(key, 0) or 0)
    if now - last < dedupe_seconds:
        return False
    state[key] = now
    return True


# --- New helpers for percentage change tracking ---
def pct_state_key(provider: str) -> str:
    return f"usage_pct_state:{provider}"


def normalize_pct(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except Exception:
        return None


def get_changed_percentage_fields(row: sqlite3.Row, state: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    provider = str(row["provider"]).lower()
    key = pct_state_key(provider)
    previous = state.get(key, {}) if isinstance(state.get(key), dict) else {}

    fields = {
        "five_hour_used_pct": "5-hour used",
        "weekly_used_pct": "Weekly used",
    }

    changed: list[tuple[str, Any, Any]] = []

    for field, label in fields.items():
        current_value = normalize_pct(row[field])
        previous_value = normalize_pct(previous.get(field))

        if current_value is None:
            continue

        if previous_value is None:
            changed.append((label, None, current_value))
        elif current_value != previous_value:
            changed.append((label, previous_value, current_value))

    return changed


def update_percentage_state(row: sqlite3.Row, state: dict[str, Any]) -> None:
    provider = str(row["provider"]).lower()
    state[pct_state_key(provider)] = {
        "observed_at": row["observed_at"],
        "five_hour_used_pct": normalize_pct(row["five_hour_used_pct"]),
        "weekly_used_pct": normalize_pct(row["weekly_used_pct"]),
    }


def main() -> None:
    env = load_env()

    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID. Add them to .env."
        )

    thresholds = DEFAULT_THRESHOLDS.copy()

    for key in list(thresholds):
        env_key = "ALERT_" + key.upper()
        if env_key in env:
            thresholds[key] = float(env[env_key])

    state = load_state()
    rows = read_forecast_rows()

    sent = 0
    suppressed = 0

    for row in rows:
        changed_fields = get_changed_percentage_fields(row, state)
        update_percentage_state(row, state)

        if not changed_fields:
            suppressed += 1
            continue

        send_telegram(token, chat_id, build_change_message(row, changed_fields))
        sent += 1

    save_state(state)

    print(f"Telegram notifications sent: {sent}")
    print(f"Suppressed duplicates:       {suppressed}")


if __name__ == "__main__":
    main()
