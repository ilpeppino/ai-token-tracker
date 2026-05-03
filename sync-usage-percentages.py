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
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / "usage.sqlite"
DUMP_DIR = PROJECT_DIR / "usage-dumps"

Provider = Literal["codex", "claude"]
RawMode = Literal["used", "remaining"]


@dataclass(frozen=True)
class ParsedUsageSnapshot:
    provider: Provider
    five_hour_used_pct: float | None
    weekly_used_pct: float | None
    raw_five_hour_pct: float | None
    raw_weekly_pct: float | None
    raw_mode: RawMode
    reset_text: str
    parser_version: str = "2026-05-03-v1"


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


def parse_codex(text: str) -> ParsedUsageSnapshot:
    five_remaining = pct_after("5 hour usage limit", text, "remaining")
    weekly_remaining = pct_after("Weekly usage limit", text, "remaining")

    return ParsedUsageSnapshot(
        provider="codex",
        five_hour_used_pct=None if five_remaining is None else 100 - five_remaining,
        weekly_used_pct=None if weekly_remaining is None else 100 - weekly_remaining,
        raw_five_hour_pct=five_remaining,
        raw_weekly_pct=weekly_remaining,
        raw_mode="remaining",
        reset_text=extract_reset_text(text),
    )


def parse_claude(text: str) -> ParsedUsageSnapshot:
    five_used = pct_after("Current session", text, "used")
    weekly_used = pct_after("Weekly limits", text, "used")

    return ParsedUsageSnapshot(
        provider="claude",
        five_hour_used_pct=five_used,
        weekly_used_pct=weekly_used,
        raw_five_hour_pct=five_used,
        raw_weekly_pct=weekly_used,
        raw_mode="used",
        reset_text=extract_reset_text(text),
    )


def detect_provider(text: str) -> Provider | None:
    low = text.lower()

    if "codex analytics" in low or "5 hour usage limit" in low and "weekly usage limit" in low:
        return "codex"

    if "plan usage limits" in low or "current session" in low and "weekly limits" in low:
        return "claude"

    return None


def parse_dump(text: str) -> ParsedUsageSnapshot | None:
    provider = detect_provider(text)

    if provider == "codex":
        return parse_codex(text)

    if provider == "claude":
        return parse_claude(text)

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

    if "reset_text" not in existing_columns:
        conn.execute("ALTER TABLE usage_percentage_snapshots ADD COLUMN reset_text TEXT")

    if "raw_text_hash" not in existing_columns:
        conn.execute("ALTER TABLE usage_percentage_snapshots ADD COLUMN raw_text_hash TEXT")

    if "parser_version" not in existing_columns:
        conn.execute("ALTER TABLE usage_percentage_snapshots ADD COLUMN parser_version TEXT")

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
          raw_text_hash,
          parser_version
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    observed_at = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(DB_PATH)

    try:
        ensure_db(conn)

        inserted = 0
        skipped_duplicates = 0
        skipped_unrecognized = 0

        for dump_file in dump_files:
            text = dump_file.read_text(errors="replace")
            parsed = parse_dump(text)

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
