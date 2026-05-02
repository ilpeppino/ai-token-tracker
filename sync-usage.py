#!/usr/bin/env python3
import json
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, date

HOME = Path.home()

DB_PATH = HOME / ".ai-token-tracker" / "usage.sqlite"

CLAUDE_DIR = HOME / ".claude"
CLAUDE_TOKEN_LOG = CLAUDE_DIR / "token-usage.jsonl"

CODEX_DB = HOME / ".codex" / "state_5.sqlite"

CLAUDE_PRICE = {
    "input": 3.00,
    "output": 15.00,
    "cache_read": 0.30,
    "cache_write": 3.75,
}

def project_from_cwd(cwd: str) -> str:
    if not cwd:
        return "?"
    return cwd.rstrip("/").split("/")[-1] or "?"

def claude_cost(input_tokens, output_tokens, cache_read, cache_write) -> float:
    return (
        input_tokens * CLAUDE_PRICE["input"] / 1_000_000
        + output_tokens * CLAUDE_PRICE["output"] / 1_000_000
        + cache_read * CLAUDE_PRICE["cache_read"] / 1_000_000
        + cache_write * CLAUDE_PRICE["cache_write"] / 1_000_000
    )

def ensure_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
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
    """)
    conn.commit()

def upsert_session(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute("""
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
    """, row)

def sync_claude(conn: sqlite3.Connection) -> int:
    if not CLAUDE_TOKEN_LOG.exists():
        return 0

    latest_by_session = {}

    with open(CLAUDE_TOKEN_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue

            sid = e.get("session_id")
            ts = e.get("timestamp")

            if not sid or not ts:
                continue

            if sid not in latest_by_session or ts > latest_by_session[sid].get("timestamp", ""):
                latest_by_session[sid] = e

    count = 0

    for sid, e in latest_by_session.items():
        ts = e.get("timestamp", "")
        day = ts[:10]
        cwd = e.get("cwd", "")

        input_tokens = int(e.get("input_tokens", 0) or 0)
        output_tokens = int(e.get("output_tokens", 0) or 0)
        cache_write = int(e.get("cache_creation_input_tokens", 0) or 0)
        cache_read = int(e.get("cache_read_input_tokens", 0) or 0)

        main_total = input_tokens + output_tokens
        full_total = input_tokens + output_tokens + cache_read + cache_write

        row = {
            "tool": "claude",
            "session_id": sid,
            "date": day,
            "timestamp": ts,
            "project": project_from_cwd(cwd),
            "cwd": cwd,
            "model": "sonnet-4.6",
            "reasoning_effort": "",
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
    return count

def sync_codex(conn: sqlite3.Connection) -> int:
    if not CODEX_DB.exists():
        return 0

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

    for r in src.execute(query):
        updated_dt = datetime.fromtimestamp(int(r["updated_at"]), tz=timezone.utc)
        ts = updated_dt.isoformat()
        day = updated_dt.date().isoformat()

        total = int(r["tokens_used"] or 0)
        cwd = r["cwd"] or ""

        row = {
            "tool": "codex",
            "session_id": r["id"],
            "date": day,
            "timestamp": ts,
            "project": project_from_cwd(cwd),
            "cwd": cwd,
            "model": r["model"] or "",
            "reasoning_effort": r["reasoning_effort"] or "",
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "main_total_tokens": total,
            "full_total_tokens": total,
            "reported_total_tokens": total,
            "cost_usd": 0.0,
            "live": 1 if codex_thread_id and r["id"] == codex_thread_id else 0,
        }

        upsert_session(conn, row)
        count += 1

    src.close()
    conn.commit()
    return count

def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    ensure_db(conn)

    claude_count = sync_claude(conn)
    codex_count = sync_codex(conn)

    total_rows = conn.execute("SELECT COUNT(*) FROM usage_sessions").fetchone()[0]
    today_rows = conn.execute(
        "SELECT COUNT(*) FROM usage_sessions WHERE date = ?",
        (date.today().isoformat(),)
    ).fetchone()[0]

    conn.close()

    print("Sync complete")
    print(f"Database: {DB_PATH}")
    print(f"Claude sessions synced: {claude_count}")
    print(f"Codex sessions synced:  {codex_count}")
    print(f"Total stored sessions:  {total_rows}")
    print(f"Today stored sessions:  {today_rows}")

if __name__ == "__main__":
    main()
