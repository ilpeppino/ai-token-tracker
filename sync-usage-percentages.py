#!/usr/bin/env python3
"""Sync browser-derived usage percentages into the tracker SQLite database.

This script ingests page-text dumps captured from authenticated browser tabs.

Current sources:
- ChatGPT Codex analytics page
- Claude usage page

It stores vendor-reported percentage snapshots in usage_percentage_snapshots.

Important:
- These values are scraped from visible browser text.
- They are best-effort and not official API values.
- Codex currently reports quota as "remaining" on the page, so the script normalizes it to "used".
- Claude currently reports quota as "used" on the page.
- Reset/window timestamps are parsed when visible and are used by calibration views when available.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / "usage.sqlite"
DUMP_DIR = PROJECT_DIR / "usage-dumps"

LOCAL_TZ = ZoneInfo("Europe/Amsterdam")

Provider = Literal["codex", "claude"]
RawMode = Literal["used", "remaining"]


@dataclass(frozen=True)
class ResetWindowInfo:
    five_hour_reset_at: str | None = None
    weekly_reset_at: str | None = None
    five_hour_window_start_at: str | None = None
    weekly_window_start_at: str | None = None


@dataclass(frozen=True)
class ParsedUsageSnapshot:
    provider: Provider
    five_hour_used_pct: float | None
    weekly_used_pct: float | None
    raw_five_hour_pct: float | None
    raw_weekly_pct: float | None
    raw_mode: RawMode
    reset_text: str
    five_hour_reset_at: str | None = None
    weekly_reset_at: str | None = None
    five_hour_window_start_at: str | None = None
    weekly_window_start_at: str | None = None
    parser_version: str = "2026-05-03-v2"


def pct_after(label: str, text: str, mode: str) -> float | None:
    """Find the first percentage after a label and a mode word.

    Example matches:
    - "5 hour usage limit ... 100% remaining"
    - "Current session ... 0% used"
    """
    pattern = rf"{re.escape(label)}.*?(\d{{1,3}}(?:\.\d+)?)%\s+{re.escape(mode)}"
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if not match:
        return None

    value = float(match.group(1))
    if value < 0 or value > 100:
        return None
    return value


def extract_reset_text(text: str) -> str:
    """Extract a compact reset-related sentence when visible in the dump."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidates: list[str] = []

    for index, line in enumerate(lines):
        low = line.lower()
        if "reset" not in low and "resets" not in low:
            continue

        context = lines[max(0, index - 1): min(len(lines), index + 2)]
        candidates.append(" | ".join(context))

    return " || ".join(candidates[:3])


def to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(timezone.utc).isoformat()


def parse_absolute_local_datetime(value: str, observed_at: datetime) -> datetime | None:
    """Parse page text like `May 5, 2026 12:27 PM` in local timezone."""
    cleaned = " ".join(value.strip().split())

    formats = [
        "%B %d, %Y %I:%M %p",
        "%b %d, %Y %I:%M %p",
        "%B %d %Y %I:%M %p",
        "%b %d %Y %I:%M %p",
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(cleaned, fmt)
            return parsed.replace(tzinfo=LOCAL_TZ)
        except ValueError:
            continue

    # Some pages may omit the year. Assume the observed year and adjust if the
    # parsed date is implausibly far in the past.
    yearless_formats = ["%B %d %I:%M %p", "%b %d %I:%M %p"]
    for fmt in yearless_formats:
        try:
            parsed = datetime.strptime(cleaned, fmt).replace(year=observed_at.year)
            parsed = parsed.replace(tzinfo=LOCAL_TZ)
            if parsed < observed_at.astimezone(LOCAL_TZ) - timedelta(days=30):
                parsed = parsed.replace(year=parsed.year + 1)
            return parsed
        except ValueError:
            continue

    return None


def parse_time_only_local_datetime(value: str, observed_at: datetime) -> datetime | None:
    """Parse page text like `11:12 PM` in local timezone."""
    cleaned = " ".join(value.strip().split())

    for fmt in ["%I:%M %p", "%H:%M"]:
        try:
            parsed_time = datetime.strptime(cleaned, fmt).time()
            local_observed = observed_at.astimezone(LOCAL_TZ)
            parsed = datetime.combine(local_observed.date(), parsed_time, tzinfo=LOCAL_TZ)

            # If the parsed time is already clearly in the past, assume tomorrow.
            if parsed < local_observed - timedelta(minutes=5):
                parsed = parsed + timedelta(days=1)

            return parsed
        except ValueError:
            continue

    return None


def parse_relative_duration(text: str) -> timedelta | None:
    """Parse relative reset text like `9 hr 14 min` or `2 hours 5 minutes`."""
    low = text.lower()

    days = hours = minutes = 0

    day_match = re.search(r"(\d+)\s*(?:d|day|days)\b", low)
    hour_match = re.search(r"(\d+)\s*(?:h|hr|hrs|hour|hours)\b", low)
    minute_match = re.search(r"(\d+)\s*(?:m|min|mins|minute|minutes)\b", low)

    if day_match:
        days = int(day_match.group(1))
    if hour_match:
        hours = int(hour_match.group(1))
    if minute_match:
        minutes = int(minute_match.group(1))

    if days == 0 and hours == 0 and minutes == 0:
        return None

    return timedelta(days=days, hours=hours, minutes=minutes)


def extract_codex_five_hour_reset(text: str, observed_at: datetime) -> datetime | None:
    """Extract Codex 5-hour reset from the 5-hour usage section."""
    match = re.search(
        r"5 hour usage limit.*?Resets\s+(\d{1,2}:\d{2}\s+[AP]M)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None

    return parse_time_only_local_datetime(match.group(1), observed_at)


def extract_claude_five_hour_relative_reset(text: str, observed_at: datetime) -> datetime | None:
    """Extract Claude current-session reset from relative text."""
    match = re.search(
        r"Current session.*?Resets in\s+([^\n]+)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None

    raw = match.group(1).strip()
    raw = re.split(r"\n|\|", raw)[0].strip()

    duration = parse_relative_duration(raw)
    if duration is None:
        return None

    return observed_at + duration


def extract_codex_absolute_reset(text: str, observed_at: datetime) -> datetime | None:
    """Extract Codex absolute reset time from page text when visible."""
    patterns = [
        r"Your limit resets on\s+([A-Za-z]+\s+\d{1,2},\s+\d{4}\s+\d{1,2}:\d{2}\s+[AP]M)",
        r"Resets\s+([A-Za-z]+\s+\d{1,2},\s+\d{4}\s+\d{1,2}:\d{2}\s+[AP]M)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        parsed = parse_absolute_local_datetime(match.group(1), observed_at)
        if parsed:
            return parsed

    return None


def extract_claude_weekly_relative_reset(text: str, observed_at: datetime) -> datetime | None:
    """Extract Claude weekly reset from relative text like `Resets in 9 hr 14 min`."""
    match = re.search(r"Weekly limits.*?Resets in\s+([^\n]+)", text, re.IGNORECASE | re.DOTALL)
    if not match:
        match = re.search(r"Resets in\s+([^\n]+)", text, re.IGNORECASE)

    if not match:
        return None

    # Stop at the next likely page label if the text came from body.innerText.
    raw = match.group(1).strip()
    raw = re.split(r"\n|\|", raw)[0].strip()

    duration = parse_relative_duration(raw)
    if duration is None:
        return None

    return observed_at + duration


def infer_reset_windows(provider: Provider, text: str, observed_at: datetime) -> ResetWindowInfo:
    """Infer reset/window timestamps from page text when possible.

    Codex exposes:
    - 5-hour reset as a local time-only value, e.g. `Resets 11:12 PM`
    - weekly reset as an absolute date/time, e.g. `May 5, 2026 12:27 PM`

    Claude exposes:
    - 5-hour reset as relative text, e.g. `Resets in 2 hr 2 min`
    - weekly reset as relative text, e.g. `Resets in 5 hr 52 min`
    """
    five_hour_reset_at: datetime | None = None
    weekly_reset_at: datetime | None = None

    if provider == "codex":
        five_hour_reset_at = extract_codex_five_hour_reset(text, observed_at)
        weekly_reset_at = extract_codex_absolute_reset(text, observed_at)

    elif provider == "claude":
        five_hour_reset_at = extract_claude_five_hour_relative_reset(text, observed_at)
        weekly_reset_at = extract_claude_weekly_relative_reset(text, observed_at)

    return ResetWindowInfo(
        five_hour_reset_at=to_utc_iso(five_hour_reset_at) if five_hour_reset_at else None,
        weekly_reset_at=to_utc_iso(weekly_reset_at) if weekly_reset_at else None,
        five_hour_window_start_at=(
            to_utc_iso(five_hour_reset_at - timedelta(hours=5)) if five_hour_reset_at else None
        ),
        weekly_window_start_at=(
            to_utc_iso(weekly_reset_at - timedelta(days=7)) if weekly_reset_at else None
        ),
    )

def parse_codex(text: str, observed_at: datetime) -> ParsedUsageSnapshot:
    five_remaining = pct_after("5 hour usage limit", text, "remaining")
    weekly_remaining = pct_after("Weekly usage limit", text, "remaining")
    reset_info = infer_reset_windows("codex", text, observed_at)

    return ParsedUsageSnapshot(
        provider="codex",
        five_hour_used_pct=None if five_remaining is None else 100 - five_remaining,
        weekly_used_pct=None if weekly_remaining is None else 100 - weekly_remaining,
        raw_five_hour_pct=five_remaining,
        raw_weekly_pct=weekly_remaining,
        raw_mode="remaining",
        reset_text=extract_reset_text(text),
        five_hour_reset_at=reset_info.five_hour_reset_at,
        weekly_reset_at=reset_info.weekly_reset_at,
        five_hour_window_start_at=reset_info.five_hour_window_start_at,
        weekly_window_start_at=reset_info.weekly_window_start_at,
    )


def parse_claude(text: str, observed_at: datetime) -> ParsedUsageSnapshot:
    five_used = pct_after("Current session", text, "used")
    weekly_used = pct_after("Weekly limits", text, "used")
    reset_info = infer_reset_windows("claude", text, observed_at)

    return ParsedUsageSnapshot(
        provider="claude",
        five_hour_used_pct=five_used,
        weekly_used_pct=weekly_used,
        raw_five_hour_pct=five_used,
        raw_weekly_pct=weekly_used,
        raw_mode="used",
        reset_text=extract_reset_text(text),
        five_hour_reset_at=reset_info.five_hour_reset_at,
        weekly_reset_at=reset_info.weekly_reset_at,
        five_hour_window_start_at=reset_info.five_hour_window_start_at,
        weekly_window_start_at=reset_info.weekly_window_start_at,
    )


def detect_provider(text: str) -> Provider | None:
    low = text.lower()

    if "codex analytics" in low or ("5 hour usage limit" in low and "weekly usage limit" in low):
        return "codex"

    if "plan usage limits" in low or ("current session" in low and "weekly limits" in low):
        return "claude"

    return None


def parse_dump(text: str, observed_at: datetime) -> ParsedUsageSnapshot | None:
    provider = detect_provider(text)

    if provider == "codex":
        return parse_codex(text, observed_at)

    if provider == "claude":
        return parse_claude(text, observed_at)

    return None


def ensure_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_percentage_snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          provider TEXT NOT NULL,
          observed_at TEXT NOT NULL,

          five_hour_used_pct REAL,
          weekly_used_pct REAL,

          raw_five_hour_pct REAL,
          raw_weekly_pct REAL,
          raw_mode TEXT,

          source TEXT NOT NULL DEFAULT 'browser_dump',
          dump_file TEXT,
          raw_text TEXT
        )
        """
    )

    # Migrate older local DBs without rebuilding user data.
    existing_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(usage_percentage_snapshots)").fetchall()
    }

    migrations = {
        "reset_text": "ALTER TABLE usage_percentage_snapshots ADD COLUMN reset_text TEXT",
        "raw_text_hash": "ALTER TABLE usage_percentage_snapshots ADD COLUMN raw_text_hash TEXT",
        "parser_version": "ALTER TABLE usage_percentage_snapshots ADD COLUMN parser_version TEXT",
        "five_hour_reset_at": "ALTER TABLE usage_percentage_snapshots ADD COLUMN five_hour_reset_at TEXT",
        "weekly_reset_at": "ALTER TABLE usage_percentage_snapshots ADD COLUMN weekly_reset_at TEXT",
        "five_hour_window_start_at": "ALTER TABLE usage_percentage_snapshots ADD COLUMN five_hour_window_start_at TEXT",
        "weekly_window_start_at": "ALTER TABLE usage_percentage_snapshots ADD COLUMN weekly_window_start_at TEXT",
    }

    for column, statement in migrations.items():
        if column not in existing_columns:
            conn.execute(statement)

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_usage_percentage_snapshots_provider_time
        ON usage_percentage_snapshots(provider, observed_at)
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_usage_percentage_snapshots_hash
        ON usage_percentage_snapshots(provider, raw_text_hash)
        """
    )

    conn.commit()


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def recently_inserted_same_snapshot(conn: sqlite3.Connection, parsed: ParsedUsageSnapshot, raw_text_hash: str) -> bool:
    """Avoid inserting the exact same dump repeatedly during quick local tests."""
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM usage_percentage_snapshots
        WHERE provider = ?
          AND raw_text_hash = ?
          AND datetime(observed_at) >= datetime('now', '-10 minutes')
        """,
        (parsed.provider, raw_text_hash),
    ).fetchone()

    return bool(row and row[0] > 0)


def insert_snapshot(
    conn: sqlite3.Connection,
    parsed: ParsedUsageSnapshot,
    dump_file: Path,
    raw_text: str,
    observed_at: str,
) -> bool:
    raw_text_hash = hash_text(raw_text)

    if recently_inserted_same_snapshot(conn, parsed, raw_text_hash):
        return False

    conn.execute(
        """
        INSERT INTO usage_percentage_snapshots (
          provider,
          observed_at,
          five_hour_used_pct,
          weekly_used_pct,
          raw_five_hour_pct,
          raw_weekly_pct,
          raw_mode,
          source,
          dump_file,
          raw_text,
          reset_text,
          five_hour_reset_at,
          weekly_reset_at,
          five_hour_window_start_at,
          weekly_window_start_at,
          raw_text_hash,
          parser_version
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            parsed.provider,
            observed_at,
            parsed.five_hour_used_pct,
            parsed.weekly_used_pct,
            parsed.raw_five_hour_pct,
            parsed.raw_weekly_pct,
            parsed.raw_mode,
            "browser_dump",
            str(dump_file),
            raw_text,
            parsed.reset_text,
            parsed.five_hour_reset_at,
            parsed.weekly_reset_at,
            parsed.five_hour_window_start_at,
            parsed.weekly_window_start_at,
            raw_text_hash,
            parsed.parser_version,
        ),
    )

    return True


def iter_dump_files() -> list[Path]:
    if not DUMP_DIR.exists():
        return []
    return sorted(DUMP_DIR.glob("*.txt"))


def main() -> None:
    dump_files = iter_dump_files()

    if not dump_files:
        raise SystemExit(f"No dump files found in: {DUMP_DIR}")

    observed_dt = datetime.now(timezone.utc)
    observed_at = observed_dt.isoformat()

    conn = sqlite3.connect(DB_PATH)

    try:
        ensure_db(conn)

        inserted = 0
        skipped_duplicates = 0
        skipped_unrecognized = 0

        for dump_file in dump_files:
            text = dump_file.read_text(errors="replace")
            parsed = parse_dump(text, observed_dt)

            if parsed is None:
                skipped_unrecognized += 1
                continue

            did_insert = insert_snapshot(conn, parsed, dump_file, text, observed_at)

            if did_insert:
                inserted += 1
                print(
                    f"{parsed.provider}: "
                    f"5h={parsed.five_hour_used_pct}% "
                    f"weekly={parsed.weekly_used_pct}% "
                    f"weekly_reset={parsed.weekly_reset_at or 'unknown'} "
                    f"mode={parsed.raw_mode} "
                    f"from {dump_file.name}"
                )
            else:
                skipped_duplicates += 1
                print(
                    f"{parsed.provider}: skipped duplicate snapshot from {dump_file.name}"
                )

        conn.commit()

        total = conn.execute(
            "SELECT COUNT(*) FROM usage_percentage_snapshots"
        ).fetchone()[0]

        print()
        print(f"Inserted snapshots:       {inserted}")
        print(f"Skipped duplicates:       {skipped_duplicates}")
        print(f"Skipped unrecognized:     {skipped_unrecognized}")
        print(f"Total snapshots stored:   {total}")
        print(f"Database:                 {DB_PATH}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
