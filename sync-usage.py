
#!/usr/bin/env python3
"""Sync local Claude Code and Codex CLI usage into the tracker SQLite database.

This script is the canonical local ingestion layer for AI Token Tracker.

It reads:
- Claude Code usage snapshots from ~/.claude/token-usage.jsonl
- Codex CLI thread usage from ~/.codex/state_5.sqlite

It writes normalized rows into usage_sessions.

Terminology:
- Toktok is this project's local, empirical usage unit.
- Toktok is not an official vendor token.
- Claude full Toktok = input + output + cache read + cache write.
- Codex full Toktok = local Codex reported total from threads.tokens_used.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

HOME = Path.home()
PROJECT_DIR = Path(__file__).resolve().parent

# Keep DB under the project folder. When the project is symlinked from
# ~/.ai-token-tracker, both paths still resolve to the same file.
DB_PATH = PROJECT_DIR / "usage.sqlite"

CLAUDE_DIR = HOME / ".claude"
CLAUDE_TOKEN_LOG = CLAUDE_DIR / "token-usage.jsonl"

CODEX_DB = HOME / ".codex" / "state_5.sqlite"

# Sonnet pricing used for estimation only. This is intentionally configurable
# in code for now and should later move to a provider/model config file.
CLAUDE_PRICE = {
    "input": 3.00,
    "output": 15.00,
    "cache_read": 0.30,
    "cache_write": 3.75,
}


@dataclass(frozen=True)
class SyncResult:
    provider: str
    synced_rows: int
    source_available: bool
    source_path: Path


def project_from_cwd(cwd: str) -> str:
    """Return a compact project name from a working directory path."""
    if not cwd:
        return "?"
    return cwd.rstrip("/").split("/")[-1] or "?"


def parse_iso_timestamp(value: str) -> datetime | None:
    """Parse common ISO timestamp formats safely.

    Claude logs can use both `Z` and explicit `+00:00` UTC suffixes.
    """
    if not value:
        return None

    normalized = value.strip().replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def timestamp_day(value: str) -> str:
    parsed = parse_iso_timestamp(value)
    if parsed:
        return parsed.date().isoformat()
    return value[:10]


def as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def claude_cost(input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> float:
    return (
        input_tokens * CLAUDE_PRICE["input"] / 1_000_000
        + output_tokens * CLAUDE_PRICE["output"] / 1_000_000
        + cache_read * CLAUDE_PRICE["cache_read"] / 1_000_000
        + cache_write * CLAUDE_PRICE["cache_write"] / 1_000_000
    )


def ensure_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_sessions (
          tool TEXT NOT NULL,
          session_id TEXT NOT NULL,
          date TEXT NOT NULL,
          timestamp TEXT NOT NULL,
          project TEXT,
          cwd TEXT,
          model TEXT,
          reasoning_effort TEXT,

          input_tokens INTEGER DEFAULT 0,
          output_tokens INTEGER DEFAULT 0,
          cache_read_tokens INTEGER DEFAULT 0,
          cache_write_tokens INTEGER DEFAULT 0,

          main_total_tokens INTEGER DEFAULT 0,
          full_total_tokens INTEGER DEFAULT 0,
          reported_total_tokens INTEGER DEFAULT 0,

          cost_usd REAL DEFAULT 0,
          live INTEGER DEFAULT 0,

          PRIMARY KEY (tool, session_id)
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_usage_sessions_date_tool
        ON usage_sessions(date, tool)
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_usage_sessions_project_tool
        ON usage_sessions(project, tool)
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_usage_sessions_timestamp
        ON usage_sessions(timestamp)
        """
    )

    conn.commit()


def upsert_session(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO usage_sessions (
          tool, session_id, date, timestamp, project, cwd, model, reasoning_effort,
          input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
          main_total_tokens, full_total_tokens, reported_total_tokens,
          cost_usd, live
        )
        VALUES (
          :tool, :session_id, :date, :timestamp, :project, :cwd, :model, :reasoning_effort,
          :input_tokens, :output_tokens, :cache_read_tokens, :cache_write_tokens,
          :main_total_tokens, :full_total_tokens, :reported_total_tokens,
          :cost_usd, :live
        )
        ON CONFLICT(tool, session_id) DO UPDATE SET
          date=excluded.date,
          timestamp=excluded.timestamp,
          project=excluded.project,
          cwd=excluded.cwd,
          model=excluded.model,
          reasoning_effort=excluded.reasoning_effort,
          input_tokens=excluded.input_tokens,
          output_tokens=excluded.output_tokens,
          cache_read_tokens=excluded.cache_read_tokens,
          cache_write_tokens=excluded.cache_write_tokens,
          main_total_tokens=excluded.main_total_tokens,
          full_total_tokens=excluded.full_total_tokens,
          reported_total_tokens=excluded.reported_total_tokens,
          cost_usd=excluded.cost_usd,
          live=excluded.live
        """,
        row,
    )


def load_latest_claude_snapshots() -> dict[str, dict[str, Any]]:
    """Load the latest Claude usage snapshot per session id."""
    latest_by_session: dict[str, dict[str, Any]] = {}

    if not CLAUDE_TOKEN_LOG.exists():
        return latest_by_session

    with CLAUDE_TOKEN_LOG.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            sid = entry.get("session_id")
            ts = entry.get("timestamp")

            if not sid or not ts:
                continue

            current = latest_by_session.get(sid)
            if current is None or ts > current.get("timestamp", ""):
                latest_by_session[sid] = entry

    return latest_by_session


def sync_claude(conn: sqlite3.Connection) -> SyncResult:
    if not CLAUDE_TOKEN_LOG.exists():
        return SyncResult("claude", 0, False, CLAUDE_TOKEN_LOG)

    latest_by_session = load_latest_claude_snapshots()
    count = 0

    for sid, entry in latest_by_session.items():
        ts = entry.get("timestamp", "")
        cwd = entry.get("cwd", "")

        input_tokens = as_int(entry.get("input_tokens"))
        output_tokens = as_int(entry.get("output_tokens"))
        cache_write = as_int(entry.get("cache_creation_input_tokens"))
        cache_read = as_int(entry.get("cache_read_input_tokens"))

        main_total = input_tokens + output_tokens
        full_total = input_tokens + output_tokens + cache_read + cache_write

        row = {
            "tool": "claude",
            "session_id": sid,
            "date": timestamp_day(ts),
            "timestamp": ts,
            "project": project_from_cwd(cwd),
            "cwd": cwd,
            "model": entry.get("model") or "sonnet-4.6",
            "reasoning_effort": entry.get("reasoning_effort") or "",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "main_total_tokens": main_total,
            "full_total_tokens": full_total,
            "reported_total_tokens": main_total,
            "cost_usd": claude_cost(input_tokens, output_tokens, cache_read, cache_write),
            "live": 0,
        }

        upsert_session(conn, row)
        count += 1

    conn.commit()
    return SyncResult("claude", count, True, CLAUDE_TOKEN_LOG)


def sync_codex(conn: sqlite3.Connection) -> SyncResult:
    if not CODEX_DB.exists():
        return SyncResult("codex", 0, False, CODEX_DB)

    codex_thread_id = os.environ.get("CODEX_THREAD_ID", "")

    src = sqlite3.connect(str(CODEX_DB))
    src.row_factory = sqlite3.Row

    count = 0

    query = """
    SELECT
      id,
      cwd,
      model,
      reasoning_effort,
      tokens_used,
      created_at,
      updated_at
    FROM threads
    """

    try:
        rows = list(src.execute(query))
    finally:
        src.close()

    for row in rows:
        updated_dt = datetime.fromtimestamp(as_int(row["updated_at"]), tz=timezone.utc)
        ts = updated_dt.isoformat()
        total = as_int(row["tokens_used"])
        cwd = row["cwd"] or ""

        normalized = {
            "tool": "codex",
            "session_id": row["id"],
            "date": updated_dt.date().isoformat(),
            "timestamp": ts,
            "project": project_from_cwd(cwd),
            "cwd": cwd,
            "model": row["model"] or "",
            "reasoning_effort": row["reasoning_effort"] or "",
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "main_total_tokens": total,
            "full_total_tokens": total,
            "reported_total_tokens": total,
            "cost_usd": 0.0,
            "live": 1 if codex_thread_id and row["id"] == codex_thread_id else 0,
        }

        upsert_session(conn, normalized)
        count += 1

    conn.commit()
    return SyncResult("codex", count, True, CODEX_DB)


def print_summary(conn: sqlite3.Connection, results: list[SyncResult]) -> None:
    total_rows = conn.execute("SELECT COUNT(*) FROM usage_sessions").fetchone()[0]
    today_rows = conn.execute(
        "SELECT COUNT(*) FROM usage_sessions WHERE date = ?",
        (date.today().isoformat(),),
    ).fetchone()[0]

    print("Sync complete")
    print(f"Database: {DB_PATH}")

    for result in results:
        status = "available" if result.source_available else "missing"
        print(
            f"{result.provider.capitalize()} sessions synced: "
            f"{result.synced_rows:<5} source={status} path={result.source_path}"
        )

    print(f"Total stored sessions:  {total_rows}")
    print(f"Today stored sessions:  {today_rows}")


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))

    try:
        ensure_db(conn)
        results = [sync_claude(conn), sync_codex(conn)]
        print_summary(conn, results)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
