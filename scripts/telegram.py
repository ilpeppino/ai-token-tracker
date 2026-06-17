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
from shutil import which
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

try:
    import cairosvg
except Exception:  # pragma: no cover - optional runtime dependency
    cairosvg = None

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
TELEGRAM_IMAGE_SIZE_PX = 320
TELEGRAM_RESIZED_ASSETS_DIR = PROJECT_DIR / ".telegram-assets"
TELEGRAM_CARD_WIDTH = 1600
TELEGRAM_CARD_HEIGHT = 320
TELEGRAM_CARD_ICON_SIZE = 118

STATUS_REFRESH_TIMEOUT_SECONDS = 90
POLL_INTERVAL_SECONDS = 5 * 60
USAGE_WARNING_THRESHOLD_PCT = 90.0
TELEGRAM_LONG_POLL_TIMEOUT_SECONDS = 25
TELEGRAM_HTTP_TIMEOUT_BUFFER_SECONDS = 10

PROVIDER_ICONS = {
    "codex": "⚪",
    "claude": "✳️",
}
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


def resized_telegram_image_path(image_path: Path) -> Path:
    return image_path
def send_photo_message(
    token: str,
    chat_id: str | int,
    image_path: Path,
    caption: str,
    *,
    parse_mode: str | None = None,
) -> None:
    if not image_path.exists():
        send_message(token, chat_id, caption, parse_mode=parse_mode)
        return

    upload_path = resized_telegram_image_path(image_path)

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
        {"photo": upload_path},
        timeout=30,
    )
def provider_image_path(provider: str) -> Path:
    key = provider.lower()
    svg_path = ASSETS_DIR / f"{key}.svg"
    if svg_path.exists():
        return svg_path
    return ASSETS_DIR / f"{key}.png"


# Load provider icon, supporting SVG and PNG
def load_provider_icon(provider: str, max_size: int) -> Any | None:
    icon_path = provider_image_path(provider)
    if not icon_path.exists() or Image is None:
        return None

    if icon_path.suffix.lower() == ".svg":
        if cairosvg is None:
            return None
        png_bytes = cairosvg.svg2png(
            url=str(icon_path),
            output_width=max_size,
            output_height=max_size,
        )
        import io

        icon = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    else:
        icon = Image.open(icon_path).convert("RGBA")

    icon.thumbnail((max_size, max_size))
    return icon
def reset_summary(row: sqlite3.Row, window: str) -> str:
    reset_at, reset_in = reset_parts(row, window)
    if reset_at == "n/a" and reset_in == "n/a":
        return "↺ n/a"
    if reset_at == "n/a":
        return f"↺ in {reset_in}"
    if reset_in == "n/a":
        return f"↺ {reset_at}"
    return f"↺ {reset_at} / {reset_in}"

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
    provider = USAGE_NOTIFY_LABELS.get(provider_name.lower(), provider_name.capitalize())
    return f"{provider:<7} {usage_dot_bar(used, width=8)} {pct_text:>4} · {reset_summary(row, window)}"


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


def usage_detail_lines(details: list[tuple[str, str]]) -> list[str]:
    label_width = max(len(label) for label, _ in details)
    return [f"{label + ':':<{label_width + 1}} {value}" for label, value in details]




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

    should_send = False

    if rounded_used == 0:
        if previous_used is not None and float(previous_used) > 0:
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

    send_provider_status(token, chat_id, row)
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
                send_provider_status(token, chat_id, row)
                state[key] = {"sent_at": now.isoformat(), "reset_at": reset_at}
                sent += 1
            else:
                pending += 1

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


def reset_text_parts(row: sqlite3.Row, window: str) -> tuple[str, str]:
    reset_at, reset_in = reset_parts(row, window)
    reset_in_text = "n/a" if reset_in == "n/a" else reset_in
    reset_at_text = reset_at
    return reset_in_text, reset_at_text


def provider_card_image_path(row: sqlite3.Row) -> Path | None:
    if Image is None or ImageDraw is None or ImageFont is None:
        return None

    TELEGRAM_RESIZED_ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    provider = str(row["provider"]).lower()
    output_path = TELEGRAM_RESIZED_ASSETS_DIR / f"{provider}-status-card.png"

    observed_dt = parse_dt(row["observed_at"])
    observed_time = (
        observed_dt.astimezone(LOCAL_TIMEZONE).strftime("%H:%M")
        if observed_dt is not None
        else "--:--"
    )

    five_hour_used = row["five_hour_used_pct"]
    weekly_used = row["weekly_used_pct"]
    five_hour_pct = clamp_percentage(five_hour_used)
    weekly_pct = clamp_percentage(weekly_used)
    five_hour_pct_text = "n/a" if five_hour_pct is None else f"{five_hour_pct:.0f}%"
    weekly_pct_text = "n/a" if weekly_pct is None else f"{weekly_pct:.0f}%"
    five_reset_in, five_reset_at = reset_text_parts(row, "five_hour")
    weekly_reset_in, weekly_reset_at = reset_text_parts(row, "weekly")

    bg = (25, 31, 39)
    panel = (34, 42, 53)
    text = (242, 245, 248)
    muted = (177, 184, 194)
    line = (61, 72, 86)
    accent = usage_rgb(five_hour_used)

    image = Image.new("RGB", (TELEGRAM_CARD_WIDTH, TELEGRAM_CARD_HEIGHT), bg)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (18, 18, TELEGRAM_CARD_WIDTH - 18, TELEGRAM_CARD_HEIGHT - 18),
        radius=34,
        fill=panel,
    )

    title_font = card_font(42, bold=True)
    label_font = card_font(24, bold=True)
    small_font = card_font(22)
    pct_font = card_font(34, bold=True)
    reset_font = card_font(24)

    icon_x = 48
    icon_y = 55
    draw.rounded_rectangle(
        (icon_x, icon_y, icon_x + TELEGRAM_CARD_ICON_SIZE, icon_y + TELEGRAM_CARD_ICON_SIZE),
        radius=22,
        fill=(244, 246, 249),
    )
    icon = load_provider_icon(provider, TELEGRAM_CARD_ICON_SIZE - 22)
    if icon is not None:
        paste_x = icon_x + (TELEGRAM_CARD_ICON_SIZE - icon.width) // 2
        paste_y = icon_y + (TELEGRAM_CARD_ICON_SIZE - icon.height) // 2
        image.paste(icon, (paste_x, paste_y), icon)

    provider_title = PROVIDER_LABELS.get(provider, provider.capitalize())
    draw.text((196, 70), provider_title, font=title_font, fill=text)
    draw.text((196, 132), f"◷ Last updated: {observed_time}", font=small_font, fill=muted)

    left_split = 390
    draw.line((left_split, 42, left_split, TELEGRAM_CARD_HEIGHT - 42), fill=line, width=2)

    row1_y = 78
    row2_y = 188
    label_x = 430
    dots_x = 635
    pct_x = 1048
    reset_x = 1172

    draw.text((label_x, row1_y), "⚡  5h usage", font=label_font, fill=text)
    draw_usage_dots(draw, dots_x, row1_y - 1, five_hour_used, width=8, radius=15, gap=13)
    draw.text((pct_x, row1_y - 5), five_hour_pct_text, font=pct_font, fill=usage_rgb(five_hour_used))
    draw.line((1138, row1_y - 14, 1138, row1_y + 50), fill=line, width=2)
    draw.text((reset_x, row1_y - 5), f"↺ {five_reset_at} / {five_reset_in}", font=reset_font, fill=accent)

    draw.line((label_x, 158, TELEGRAM_CARD_WIDTH - 58, 158), fill=line, width=2)

    draw.text((label_x, row2_y), "📅  Weekly usage", font=label_font, fill=text)
    draw_usage_dots(draw, dots_x, row2_y - 1, weekly_used, width=8, radius=15, gap=13)
    draw.text((pct_x, row2_y - 5), weekly_pct_text, font=pct_font, fill=usage_rgb(weekly_used))
    draw.line((1138, row2_y - 14, 1138, row2_y + 50), fill=line, width=2)
    draw.text((reset_x, row2_y - 5), f"↺ {weekly_reset_at} / {weekly_reset_in}", font=reset_font, fill=usage_rgb(weekly_used))

    image.save(output_path, "PNG")
    return output_path


def build_provider_card(row: sqlite3.Row) -> str:
    observed_dt = parse_dt(row["observed_at"])
    observed_time = (
        observed_dt.astimezone(LOCAL_TIMEZONE).strftime("%H:%M")
        if observed_dt is not None
        else "--:--"
    )

    five_hour_used = row["five_hour_used_pct"]
    weekly_used = row["weekly_used_pct"]
    five_hour_pct = clamp_percentage(five_hour_used)
    weekly_pct = clamp_percentage(weekly_used)
    five_hour_pct_text = "n/a" if five_hour_pct is None else f"{five_hour_pct:.0f}%"
    weekly_pct_text = "n/a" if weekly_pct is None else f"{weekly_pct:.0f}%"

    lines = [
        f"🕒 Last updated: {observed_time}",
        "",
        f"⚡ 5h usage     {usage_dot_bar(five_hour_used, width=8)} {five_hour_pct_text:>4} · {reset_summary(row, 'five_hour')}",
        f"📅 Weekly usage {usage_dot_bar(weekly_used, width=8)} {weekly_pct_text:>4} · {reset_summary(row, 'weekly')}",
    ]
    return "<pre>" + escape("\n".join(lines).strip()) + "</pre>"


def send_provider_status(token: str, chat_id: str | int, row: sqlite3.Row) -> None:
    card_path = provider_card_image_path(row)
    if card_path is not None and card_path.exists():
        send_photo_message(token, chat_id, card_path, "")
        return

    provider = str(row["provider"]).lower()
    caption = build_provider_card(row)
    send_photo_message(
        token,
        chat_id,
        provider_image_path(provider),
        caption,
        parse_mode="HTML",
    )

# New: send_status_cards
def send_status_cards(
    token: str,
    chat_id: str | int,
    provider: str | None = None,
    refresh_note: str | None = None,
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

    if refresh_note:
        send_message(token, chat_id, refresh_note, parse_mode="HTML")

    for row in rows:
        send_provider_status(token, chat_id, row)


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


def handle_command(token: str, chat_id: str | int, text: str) -> None:
    cmd = text.strip().split()[0].lower()

    if cmd in {"/start", "/help"}:
        send_message(token, chat_id, help_text(), parse_mode="HTML")
        return

    if cmd in {"/status", "/forecast"}:
        ok, note = refresh_usage_before_status()
        send_status_cards(token, chat_id, refresh_note=freshness_note(ok, note))
        return

    if cmd == "/codex":
        ok, note = refresh_usage_before_status()
        send_status_cards(token, chat_id, "codex", refresh_note=freshness_note(ok, note))
        return

    if cmd == "/claude":
        ok, note = refresh_usage_before_status()
        send_status_cards(token, chat_id, "claude", refresh_note=freshness_note(ok, note))
        return

    send_message(token, chat_id, "Unknown command. Send <code>/help</code>.", parse_mode="HTML")


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

            handle_command(token, chat_id, text)

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
