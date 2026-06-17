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
TELEGRAM_CARD_WIDTH = 1080
TELEGRAM_CARD_HEIGHT = 1080
TELEGRAM_CARD_INNER_WIDTH = 940
TELEGRAM_CARD_INNER_HEIGHT = 760
TELEGRAM_CARD_ICON_SIZE = 132
TELEGRAM_CARD_RENDER_ERROR = (
    "Card rendering requires Pillow. Install it with: "
    "<code>source .venv/bin/activate && python -m pip install pillow cairosvg</code>"
)

STATUS_REFRESH_TIMEOUT_SECONDS = 90
POLL_INTERVAL_SECONDS = 5 * 60
USAGE_WARNING_THRESHOLD_PCT = 90.0
TELEGRAM_LONG_POLL_TIMEOUT_SECONDS = 25
TELEGRAM_HTTP_TIMEOUT_BUFFER_SECONDS = 10

PROVIDER_ICONS = {
    "codex": "⚪",
    "claude": "✳️",
}

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


def resized_telegram_image_path(image_path: Path) -> Path:
    return image_path


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

    try:
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
    except Exception:
        return None

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
    five_reset_in, _five_reset_at = reset_text_parts(row, "five_hour")
    weekly_reset_in, _weekly_reset_at = reset_text_parts(row, "weekly")

    bg = (13, 20, 28)
    panel = (31, 40, 51)
    panel_inner = (35, 45, 57)
    text = (245, 247, 250)
    muted = (183, 190, 200)
    line = (54, 66, 80)

    image = Image.new("RGB", (TELEGRAM_CARD_WIDTH, TELEGRAM_CARD_HEIGHT), bg)
    draw = ImageDraw.Draw(image)

    card_left = (TELEGRAM_CARD_WIDTH - TELEGRAM_CARD_INNER_WIDTH) // 2
    card_top = (TELEGRAM_CARD_HEIGHT - TELEGRAM_CARD_INNER_HEIGHT) // 2
    card_right = card_left + TELEGRAM_CARD_INNER_WIDTH
    card_bottom = card_top + TELEGRAM_CARD_INNER_HEIGHT

    draw.rounded_rectangle(
        (card_left, card_top, card_right, card_bottom),
        radius=46,
        fill=panel,
    )
    draw.rounded_rectangle(
        (card_left + 8, card_top + 8, card_right - 8, card_bottom - 8),
        radius=40,
        fill=panel_inner,
    )

    title_font = card_font(58, bold=True)
    label_font = card_font(38, bold=True)
    small_font = card_font(28)
    pct_font = card_font(48, bold=True)
    reset_font = card_font(32)
    fallback_font = card_font(76, bold=True)

    icon_x = card_left + 54
    icon_y = card_top + 54
    draw.rounded_rectangle(
        (icon_x, icon_y, icon_x + TELEGRAM_CARD_ICON_SIZE, icon_y + TELEGRAM_CARD_ICON_SIZE),
        radius=28,
        fill=provider_icon_background(provider),
    )
    icon = load_provider_icon(provider, TELEGRAM_CARD_ICON_SIZE - 24)
    if icon is not None:
        paste_x = icon_x + (TELEGRAM_CARD_ICON_SIZE - icon.width) // 2
        paste_y = icon_y + (TELEGRAM_CARD_ICON_SIZE - icon.height) // 2
        image.paste(icon, (paste_x, paste_y), icon)
    else:
        fallback = provider_icon_fallback(provider)
        bbox = draw.textbbox((0, 0), fallback, font=fallback_font)
        fallback_w = bbox[2] - bbox[0]
        fallback_h = bbox[3] - bbox[1]
        draw.text(
            (
                icon_x + (TELEGRAM_CARD_ICON_SIZE - fallback_w) / 2,
                icon_y + (TELEGRAM_CARD_ICON_SIZE - fallback_h) / 2 - 6,
            ),
            fallback,
            font=fallback_font,
            fill=provider_icon_fallback_color(provider),
        )

    provider_title = PROVIDER_LABELS.get(provider, provider.capitalize())
    draw.text((card_left + 220, card_top + 62), provider_title, font=title_font, fill=text)
    draw.text((card_left + 224, card_top + 148), f"◷ Last updated: {observed_time}", font=small_font, fill=muted)

    header_bottom = card_top + 238
    draw.line((card_left + 54, header_bottom, card_right - 54, header_bottom), fill=line, width=3)

    def draw_usage_row(
        *,
        y: int,
        label: str,
        used: Any,
        pct_text: str,
        reset_in: str,
    ) -> None:
        usage_color = usage_rgb(used)
        label_x = card_left + 70
        dots_x = card_left + 70
        pct_x = card_right - 210
        reset_x = card_left + 70

        draw.text((label_x, y), label, font=label_font, fill=text)
        draw_usage_dots(draw, dots_x, y + 74, used, width=8, radius=25, gap=17)
        draw.text((pct_x, y + 60), pct_text, font=pct_font, fill=usage_color)
        draw.text((reset_x, y + 148), f"Resets in {reset_in}", font=reset_font, fill=usage_color)

    draw_usage_row(
        y=card_top + 285,
        label="⚡  5h Usage",
        used=five_hour_used,
        pct_text=five_hour_pct_text,
        reset_in=five_reset_in,
    )

    middle_line = card_top + 500
    draw.line((card_left + 54, middle_line, card_right - 54, middle_line), fill=line, width=3)

    draw_usage_row(
        y=card_top + 545,
        label="📅  Weekly Usage",
        used=weekly_used,
        pct_text=weekly_pct_text,
        reset_in=weekly_reset_in,
    )

    image.save(output_path, "PNG")
    return output_path


def build_provider_card(row: sqlite3.Row) -> str:
    card_path = provider_card_image_path(row)
    if card_path is None:
        return TELEGRAM_CARD_RENDER_ERROR
    return str(card_path)


def send_provider_status(token: str, chat_id: str | int, row: sqlite3.Row) -> None:
    card_path = provider_card_image_path(row)
    if card_path is None:
        send_message(token, chat_id, TELEGRAM_CARD_RENDER_ERROR, parse_mode="HTML")
        return
    if not card_path.exists():
        send_message(token, chat_id, "Telegram status card could not be generated.")
        return

    send_photo_message(token, chat_id, card_path)

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


def freshness_note(success: bool, message: str) -> str:
    if not message:
        return ""
    return message


def build_status(provider: str | None = None, refresh_note: str | None = None) -> str:
    rows = read_forecast_rows(provider)
    if not rows:
        return "No forecast data found. Run ai-tokens sync first."

    lines: list[str] = []
    if refresh_note:
        lines.append(refresh_note)
    for row in rows:
        lines.append(status_line(row, "five_hour"))
        lines.append(status_line(row, "weekly"))
    return "\n".join(lines)


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
            "  poll-notify   Check usage-change notifications every 5 minutes.",
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
