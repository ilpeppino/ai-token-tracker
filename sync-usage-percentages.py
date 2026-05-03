#!/usr/bin/env python3
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

BASE = Path("/Volumes/DevSSD/projects/ai-token-tracker")
DB_PATH = BASE / "usage.sqlite"
DUMP_DIR = BASE / "usage-dumps"

def pct_after(label, text, mode):
    pattern = rf"{re.escape(label)}.*?(\d{{1,3}})%\s+{mode}"
    m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    return int(m.group(1)) if m else None

def parse_codex(text):
    five_remaining = pct_after("5 hour usage limit", text, "remaining")
    weekly_remaining = pct_after("Weekly usage limit", text, "remaining")

    return {
        "provider": "codex",
        "five_hour_used_pct": None if five_remaining is None else 100 - five_remaining,
        "weekly_used_pct": None if weekly_remaining is None else 100 - weekly_remaining,
        "raw_five_hour_pct": five_remaining,
        "raw_weekly_pct": weekly_remaining,
        "raw_mode": "remaining",
    }

def parse_claude(text):
    five_used = pct_after("Current session", text, "used")
    weekly_used = pct_after("Weekly limits", text, "used")

    return {
        "provider": "claude",
        "five_hour_used_pct": five_used,
        "weekly_used_pct": weekly_used,
        "raw_five_hour_pct": five_used,
        "raw_weekly_pct": weekly_used,
        "raw_mode": "used",
    }

def ensure_db(conn):
    conn.execute("""
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
    """)

    conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_usage_percentage_snapshots_provider_time
    ON usage_percentage_snapshots(provider, observed_at)
    """)

    conn.commit()

def insert_snapshot(conn, parsed, dump_file, raw_text):
    conn.execute("""
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
      raw_text
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        parsed["provider"],
        datetime.now(timezone.utc).isoformat(),
        parsed["five_hour_used_pct"],
        parsed["weekly_used_pct"],
        parsed["raw_five_hour_pct"],
        parsed["raw_weekly_pct"],
        parsed["raw_mode"],
        "browser_dump",
        str(dump_file),
        raw_text,
    ))

def main():
    if not DUMP_DIR.exists():
        raise SystemExit(f"Dump folder not found: {DUMP_DIR}")

    conn = sqlite3.connect(DB_PATH)
    ensure_db(conn)

    inserted = 0

    for file in sorted(DUMP_DIR.glob("*.txt")):
        text = file.read_text(errors="replace")
        low = text.lower()

        parsed = None

        if "codex analytics" in low:
            parsed = parse_codex(text)
        elif "plan usage limits" in low:
            parsed = parse_claude(text)

        if not parsed:
            continue

        insert_snapshot(conn, parsed, file, text)
        inserted += 1

        print(
            f"{parsed['provider']}: "
            f"5h={parsed['five_hour_used_pct']}% "
            f"weekly={parsed['weekly_used_pct']}% "
            f"from {file.name}"
        )

    conn.commit()

    total = conn.execute(
        "SELECT COUNT(*) FROM usage_percentage_snapshots"
    ).fetchone()[0]

    conn.close()

    print()
    print(f"Inserted snapshots: {inserted}")
    print(f"Total snapshots stored: {total}")

if __name__ == "__main__":
    main()
