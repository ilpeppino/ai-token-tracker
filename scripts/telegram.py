#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shlex
import sqlite3
import subprocess
import sys
import signal
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

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover - optional runtime dependency
    Image = None
    ImageDraw = None
    ImageFont = None

PROJECT_DIR = Path(__file__).resolve().parents[1]
ASSETS_DIR = PROJECT_DIR / "assets"
DB_PATH = PROJECT_DIR / "usage.sqlite"
ENV_PATH = PROJECT_DIR / ".env"
AI_TOKENS_SCRIPT = PROJECT_DIR / "ai-tokens"
LOCAL_TIMEZONE = ZoneInfo("Europe/Amsterdam")

RESET_STATE_PATH = PROJECT_DIR / ".reset-notify-state.json"
NOTIFY_STATE_PATH = PROJECT_DIR / ".notify-state.json"
BOT_STATE_PATH = PROJECT_DIR / ".telegram-bot-state.json"
TELEGRAM_UPLOAD_BOUNDARY = "----ai-token-tracker-boundary"
TELEGRAM_RESIZED_ASSETS_DIR = PROJECT_DIR / ".telegram-assets"
TELEGRAM_CARD_WIDTH = 1080
TELEGRAM_CARD_HEIGHT = 1080
TELEGRAM_CARD_RENDER_ERROR = (
    "Card rendering requires Pillow. Install it with:\n"
    "source .venv/bin/activate && python -m pip install pillow"
)

STATUS_REFRESH_TIMEOUT_SECONDS = 90
POLL_INTERVAL_SECONDS = 15 * 60
USAGE_WARNING_THRESHOLD_PCT = 90.0
TELEGRAM_LONG_POLL_TIMEOUT_SECONDS = 25
TELEGRAM_HTTP_TIMEOUT_BUFFER_SECONDS = 10

STOP_EVENT = threading.Event()


def configured_command(env_name: str) -> list[str] | None:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return None
    return shlex.split(raw)


def default_refresh_command() -> list[str] | None:
    return [str(AI_TOKENS_SCRIPT), "sync"]


def telegram_refresh_command() -> list[str] | None:
    return configured_command("AI_TOKENS_TELEGRAM_REFRESH_COMMAND") or default_refresh_command()


def telegram_poll_sync_command() -> list[str] | None:
    return configured_command("AI_TOKENS_TELEGRAM_SYNC_COMMAND") or default_refresh_command()


def run_command(command: list[str], *, timeout: int) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"{command[0]} timed out after {timeout}s"
    except Exception as exc:
        return False, f"{command[0]} failed: {exc}"

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        if len(detail) > 500:
            detail = detail[:500] + "…"
        return False, detail

    output = (result.stdout or "").strip()
    if not output:
        output = f"{command[0]} complete"
    return True, output


def install_signal_handlers() -> None:
    def _handle_signal(signum: int, frame: Any) -> None:
        STOP_EVENT.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except Exception:
            pass


def telegram_api_multipart(
    token: str,
    method: str,
    fields: dict[str, Any],
    files: dict[str, Path],
    timeout: int = 30,
) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    boundary = TELEGRAM_UPLOAD_BOUNDARY
    body = bytearray()

    for key, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(
                "utf-8"
            )
        )
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    for key, path in files.items():
        filename = path.name
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{key}"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8")
        )
        body.extend(b"Content-Type: image/png\r\n\r\n")
        body.extend(path.read_bytes())
        body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    request = urllib.request.Request(
        url,
        data=bytes(body),
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
        if response.status >= 300:
            raise RuntimeError(payload)
        return json.loads(payload)


def send_photo_message(
    token: str,
    chat_id: str | int,
    image_path: Path,
    caption: str = "",
    *,
    parse_mode: str | None = None,
) -> None:
    if not image_path.exists():
        if caption:
            send_message(token, chat_id, caption, parse_mode=parse_mode)
        return

    fields: dict[str, Any] = {
        "chat_id": str(chat_id),
    }
    if caption:
        fields["caption"] = caption
    if parse_mode:
        fields["parse_mode"] = parse_mode

    telegram_api_multipart(
        token,
        "sendPhoto",
        fields,
        {"photo": image_path},
        timeout=30,
    )


def provider_image_path(provider: str) -> Path:
    if provider.lower() == "codex":
        return ASSETS_DIR / "codex-cloud.png"
    return ASSETS_DIR / f"{provider.lower()}.png"


def load_provider_icon(provider: str, max_size: int) -> Any | None:
    icon_path = provider_image_path(provider)
    if not icon_path.exists() or Image is None:
        return None

    try:
        icon = Image.open(icon_path).convert("RGBA")
    except Exception:
        return None

    icon.thumbnail((max_size, max_size))
    return icon


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


def clamp_percentage(value: Any) -> float | None:
    try:
        return max(0.0, min(100.0, float(value)))
    except Exception:
        return None


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


def read_forecast_rows(provider: str | None = None) -> list[sqlite3.Row]:
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        forecast_exists = conn.execute(
            """
            SELECT COUNT(*)
            FROM sqlite_master
            WHERE type = 'view'
              AND name = 'quota_forecast'
            """
        ).fetchone()[0] > 0
        if not forecast_exists:
            return []

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
    except sqlite3.OperationalError as exc:
        if "calibration_estimates" in str(exc) or "quota_forecast" in str(exc):
            return []
        raise
    finally:
        conn.close()


def notify_for_usage_row(
    token: str,
    chat_id: str,
    state: dict[str, Any],
    row: sqlite3.Row,
    *,
    send: bool = True,
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

    should_send = False

    if rounded_used == 0:
        if previous_used is not None and float(previous_used) > 0:
            # Skip here when run_reset_notify will send the same card
            if row["five_hour_reset_status"] != "known_reset":
                should_send = True
        provider_state["five_hour_warning_sent"] = False
    elif rounded_used >= USAGE_WARNING_THRESHOLD_PCT and not previous_warning_sent:
        should_send = True
        provider_state["five_hour_warning_sent"] = True
    elif previous_used is not None and rounded_used != round(float(previous_used)):
        should_send = True
        if rounded_used < USAGE_WARNING_THRESHOLD_PCT:
            provider_state["five_hour_warning_sent"] = False
    elif previous_used is None:
        should_send = True
        provider_state["five_hour_warning_sent"] = rounded_used >= USAGE_WARNING_THRESHOLD_PCT

    provider_state["five_hour_used_pct"] = float(rounded_used)
    provider_state["five_hour_reset_at"] = reset_at
    provider_state["observed_at"] = row["observed_at"]

    if not should_send:
        return False

    if send:
        send_status_image(token, chat_id, read_forecast_rows(), reason="usage-change")
        provider_state["last_sent_at"] = datetime.now(timezone.utc).isoformat()
    return True


def run_notify() -> None:
    token, chat_id = telegram_credentials()
    state = load_state(NOTIFY_STATE_PATH)

    changed_states: list[dict[str, Any]] = []
    for row in read_forecast_rows():
        if notify_for_usage_row(token, chat_id, state, row, send=False):
            changed_states.append(state[str(row["provider"]).lower()])

    sent = 0
    if changed_states:
        send_status_image(token, chat_id, read_forecast_rows(), reason="usage-change")
        sent_at = datetime.now(timezone.utc).isoformat()
        for provider_state in changed_states:
            provider_state["last_sent_at"] = sent_at
        sent = 1

    save_state(NOTIFY_STATE_PATH, state)
    print(f"Usage notifications sent: {sent}")




def run_reset_notify() -> None:
    token, chat_id = telegram_credentials()
    state = load_state(RESET_STATE_PATH)
    now = datetime.now(timezone.utc)

    due_keys: list[tuple[str, Any]] = []
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
                due_keys.append((key, reset_at))
            else:
                pending += 1

    sent = 0
    if due_keys:
        send_status_image(token, chat_id, read_forecast_rows(), reason="reset")
        for key, reset_at in due_keys:
            state[key] = {"sent_at": now.isoformat(), "reset_at": reset_at}
        sent = 1

    save_state(RESET_STATE_PATH, state)

    print(f"Reset notifications sent: {sent}")
    print(f"Known future resets pending: {pending}")


def card_font(size: int, *, bold: bool = False) -> Any:
    if ImageFont is None:
        return None

    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def dot_fill_count(used_pct: Any, width: int = 8) -> int:
    pct = clamp_percentage(used_pct)
    if pct is None or pct <= 0:
        return 0
    if pct >= 100:
        return width
    return max(1, min(width - 1, round((pct / 100.0) * width)))


def usage_rgb(used_pct: Any) -> tuple[int, int, int]:
    pct = clamp_percentage(used_pct)
    if pct is None:
        return (145, 151, 160)
    if pct >= USAGE_WARNING_THRESHOLD_PCT:
        return (245, 58, 72)
    if pct >= 70:
        return (255, 132, 67)
    if pct >= 30:
        return (252, 190, 62)
    return (67, 201, 92)


# --- Provider icon helpers ---
def provider_icon_background(provider: str) -> tuple[int, int, int]:
    key = provider.lower()
    if key == "claude":
        return (221, 103, 64)
    return (247, 248, 250)


def provider_icon_fallback(provider: str) -> str:
    key = provider.lower()
    if key == "claude":
        return "✳"
    if key == "codex":
        return "◎"
    return "AI"


def provider_icon_fallback_color(provider: str) -> tuple[int, int, int]:
    key = provider.lower()
    if key == "claude":
        return (255, 255, 255)
    return (20, 24, 30)


def draw_usage_dots(
    draw: Any,
    x: int,
    y: int,
    used_pct: Any,
    *,
    width: int = 8,
    radius: int = 18,
    gap: int = 12,
) -> None:
    filled = dot_fill_count(used_pct, width)
    active = usage_rgb(used_pct)
    inactive = (145, 151, 160)
    for index in range(width):
        cx = x + index * ((radius * 2) + gap)
        color = active if index < filled else inactive
        draw.ellipse((cx, y, cx + radius * 2, y + radius * 2), fill=color)


def build_status_caption(rows: list[sqlite3.Row]) -> str:
    lines = []
    for row in rows:
        provider = str(row["provider"]).lower()
        if provider not in {"codex", "claude"}:
            continue
        label = PROVIDER_LABELS.get(provider, provider.capitalize())
        lines.append(
            f"{label}: 5h {fmt_pct(row['five_hour_used_pct'])}, "
            f"weekly {fmt_pct(row['weekly_used_pct'])}"
        )
    return "\n".join(lines)


def provider_card_draw(
    image: Any,
    draw: Any,
    row: sqlite3.Row,
    bounds: tuple[int, int, int, int],
) -> None:
    left, top, right, bottom = bounds
    provider = str(row["provider"]).lower()
    text = (245, 247, 250)
    muted = (177, 186, 198)
    line = (58, 70, 84)
    draw.rounded_rectangle(bounds, radius=38, fill=(31, 40, 51))
    draw.rounded_rectangle((left + 5, top + 5, right - 5, bottom - 5), radius=34, fill=(35, 45, 57))

    icon_size = 92
    icon_x, icon_y = left + 40, top + 34
    draw.rounded_rectangle(
        (icon_x, icon_y, icon_x + icon_size, icon_y + icon_size),
        radius=22,
        fill=provider_icon_background(provider),
    )
    icon = load_provider_icon(provider, icon_size - 16)
    if icon is not None:
        image.paste(
            icon,
            (icon_x + (icon_size - icon.width) // 2, icon_y + (icon_size - icon.height) // 2),
            icon,
        )
    else:
        fallback = provider_icon_fallback(provider)
        fallback_font = card_font(50, bold=True)
        bbox = draw.textbbox((0, 0), fallback, font=fallback_font)
        draw.text(
            (icon_x + (icon_size - (bbox[2] - bbox[0])) / 2, icon_y + 13),
            fallback,
            font=fallback_font,
            fill=provider_icon_fallback_color(provider),
        )

    observed = parse_dt(row["observed_at"])
    observed_text = observed.astimezone(LOCAL_TIMEZONE).strftime("%d %b, %H:%M") if observed else "unknown"
    draw.text((left + 160, top + 32), PROVIDER_LABELS.get(provider, provider.capitalize()), font=card_font(46, bold=True), fill=text)
    draw.text((left + 162, top + 91), f"Last updated {observed_text}", font=card_font(24), fill=muted)
    draw.line((left + 38, top + 145, right - 38, top + 145), fill=line, width=2)

    def usage_row(y: int, label: str, used: Any, window: str) -> None:
        color = usage_rgb(used)
        draw.text((left + 48, y), label, font=card_font(30, bold=True), fill=text)
        draw_usage_dots(draw, left + 48, y + 50, used, width=8, radius=17, gap=14)
        draw.text((right - 148, y + 37), fmt_pct(used), font=card_font(38, bold=True), fill=color)
        reset_at, reset_in = reset_parts(row, window)
        detail = f"Resets {reset_at} (in {reset_in})" if reset_at != "n/a" else f"Resets in {reset_in}"
        draw.text((left + 48, y + 94), detail, font=card_font(23), fill=muted)

    usage_row(top + 168, "5h usage", row["five_hour_used_pct"], "five_hour")
    draw.line((left + 38, top + 318, right - 38, top + 318), fill=line, width=2)
    usage_row(top + 338, "Weekly usage", row["weekly_used_pct"], "weekly")


def combined_status_image_path(
    rows: list[sqlite3.Row],
    reason: str = "status",
) -> Path | None:
    if Image is None or ImageDraw is None or ImageFont is None:
        return None
    if not rows:
        return None

    rows = sorted(
        (row for row in rows if str(row["provider"]).lower() in {"codex", "claude"}),
        key=lambda row: str(row["provider"]).lower(),
    )
    if not rows:
        return None
    TELEGRAM_RESIZED_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    observed_values = [parse_dt(row["observed_at"]) for row in rows]
    observed = max((value for value in observed_values if value is not None), default=None)
    stamp = (observed or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S")
    providers = "-".join(str(row["provider"]).lower() for row in rows)
    safe_reason = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in reason.lower())
    output_path = TELEGRAM_RESIZED_ASSETS_DIR / (
        f"{safe_reason}-{providers}-{TELEGRAM_CARD_WIDTH}x{TELEGRAM_CARD_HEIGHT}-{stamp}.png"
    )

    image = Image.new("RGB", (TELEGRAM_CARD_WIDTH, TELEGRAM_CARD_HEIGHT), (13, 20, 28))
    draw = ImageDraw.Draw(image)
    if len(rows) == 1:
        bounds = [(60, 278, 1020, 802)]
    else:
        card_height = 486
        gap = 28
        total_height = len(rows) * card_height + (len(rows) - 1) * gap
        top = max(28, (TELEGRAM_CARD_HEIGHT - total_height) // 2)
        bounds = [(60, top + i * (card_height + gap), 1020, top + i * (card_height + gap) + card_height) for i in range(len(rows))]

    for row, card_bounds in zip(rows, bounds):
        provider_card_draw(image, draw, row, card_bounds)
    image.save(output_path, "PNG")
    print(f"Generated Telegram card: {output_path} ({TELEGRAM_CARD_WIDTH}x{TELEGRAM_CARD_HEIGHT})", flush=True)
    return output_path


def send_status_image(
    token: str,
    chat_id: str | int,
    rows: list[sqlite3.Row],
    reason: str = "status",
) -> None:
    card_path = combined_status_image_path(rows, reason)
    if card_path is None:
        send_message(token, chat_id, TELEGRAM_CARD_RENDER_ERROR)
        return
    print(f"Sending Telegram photo: {card_path}", flush=True)
    send_photo_message(token, chat_id, card_path, build_status_caption(rows))


def send_status_cards(
    token: str,
    chat_id: str | int,
    provider: str | None = None,
) -> None:
    rows = read_forecast_rows(provider)
    if not rows:
        send_message(
            token,
            chat_id,
            "No forecast data found. Run <code>ai-tokens sync</code> first.",
            parse_mode="HTML",
        )
        return

    send_status_image(token, chat_id, rows, reason="status")


def refresh_usage_before_status() -> tuple[bool, str]:
    command = telegram_refresh_command()
    if command is None:
        return False, "Using cached local data."

    ok, detail = run_command(command, timeout=STATUS_REFRESH_TIMEOUT_SECONDS)
    if ok:
        return True, "Refreshed just now."

    detail = detail.strip()
    if len(detail) > 240:
        detail = detail[:240] + "…"
    return False, f"Sync failed. Showing last cached data. <code>{escape(detail)}</code>"


def run_usage_sync() -> tuple[bool, str]:
    command = telegram_poll_sync_command()
    if command is None:
        return True, "Sync skipped on this platform."

    return run_command(command, timeout=STATUS_REFRESH_TIMEOUT_SECONDS)


def run_poll_notify(*, include_reset_notify: bool = False) -> None:
    print("Telegram usage polling started. Press Ctrl+C to stop.", flush=True)

    while not STOP_EVENT.is_set():
        started = time.monotonic()
        ok, detail = run_usage_sync()
        if not ok:
            print(f"Usage sync failed before notify: {detail}", flush=True)
        else:
            run_notify()
            if include_reset_notify:
                run_reset_notify()

        elapsed = time.monotonic() - started
        remaining = max(1.0, POLL_INTERVAL_SECONDS - elapsed)
        STOP_EVENT.wait(remaining)


def run_telegram() -> None:
    install_signal_handlers()
    poll_thread = threading.Thread(
        target=run_poll_notify,
        kwargs={"include_reset_notify": True},
        daemon=True,
    )
    poll_thread.start()
    run_bot()


def help_text() -> str:
    return "\n".join(
        [
            "<b>AI Token Tracker commands</b>",
            "",
            "<code>/status</code> - Show Codex + Claude status",
            "<code>/forecast</code> - Same as status",
            "<code>/codex</code> - Show Codex only",
            "<code>/claude</code> - Show Claude only",
            "<code>/help</code> - Command list",
        ]
    )


def handle_command(token: str, chat_id: str | int, text: str) -> None:
    cmd = text.strip().split()[0].lower()

    if cmd in {"/start", "/help"}:
        send_message(token, chat_id, help_text(), parse_mode="HTML")
        return

    if cmd in {"/status", "/forecast"}:
        refresh_usage_before_status()
        send_status_cards(token, chat_id)
        return

    if cmd == "/codex":
        refresh_usage_before_status()
        send_status_cards(token, chat_id, "codex")
        return

    if cmd == "/claude":
        refresh_usage_before_status()
        send_status_cards(token, chat_id, "claude")
        return

    send_message(token, chat_id, "Unknown command. Send <code>/help</code>.", parse_mode="HTML")


def run_bot() -> None:
    token, allowed_chat_id = telegram_credentials()
    state = load_state(BOT_STATE_PATH, {"offset": 0})
    offset = int(state.get("offset", 0) or 0)

    print("Telegram bot polling started. Press Ctrl+C to stop.")

    while not STOP_EVENT.is_set():
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

            handle_command(token, chat_id, text)

        STOP_EVENT.wait(1)


def usage() -> str:
    return "\n".join(
        [
            "Usage: scripts/telegram.py [notify|poll-notify|reset-notify|bot|telegram|service]",
            "",
            "Commands:",
            "  notify        Send usage-change notifications once.",
            "  poll-notify   Check usage-change notifications every 15 minutes.",
            "  reset-notify  Send reset notifications once.",
            "  bot           Run the interactive Telegram bot.",
            "  telegram      Run the interactive bot and usage polling together.",
            "  service       Alias for telegram, intended for systemd.",
        ]
    )


def main() -> None:
    install_signal_handlers()
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
    if command == "service":
        run_telegram()
        return

    raise SystemExit(usage())


if __name__ == "__main__":
    main()
