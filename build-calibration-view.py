#!/usr/bin/env python3
"""Build SQLite calibration views for Toktok ↔ vendor usage percentage estimates.

This script creates/refreshes the `calibration_estimates` view.

The view joins:
- local usage rows from `usage_sessions`
- vendor percentage snapshots from `usage_percentage_snapshots`

It estimates how many Toktok correspond to observed 5-hour and weekly usage percentages.

Important:
- These are empirical estimates, not official vendor quota values.
- The 5-hour estimate currently uses local usage from the last 5 hours before a snapshot.
- The weekly estimate currently uses local usage from the last 7 days before a snapshot.
- Future versions can replace those rolling windows with exact vendor reset-window boundaries when available.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / "usage.sqlite"

VIEW_NAME = "calibration_estimates"


def ensure_required_tables(conn: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    }

    missing = []
    if "usage_sessions" not in tables:
        missing.append("usage_sessions")
    if "usage_percentage_snapshots" not in tables:
        missing.append("usage_percentage_snapshots")

    if missing:
        raise SystemExit(
            "Missing required database objects: "
            + ", ".join(missing)
            + ". Run sync-usage.py and sync-usage-percentages.py first."
        )


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def create_calibration_view(conn: sqlite3.Connection) -> None:
    conn.execute(f"DROP VIEW IF EXISTS {VIEW_NAME}")

    snapshot_columns = table_columns(conn, "usage_percentage_snapshots")

    five_hour_reset_expr = (
        "five_hour_reset_at"
        if "five_hour_reset_at" in snapshot_columns
        else "NULL AS five_hour_reset_at"
    )
    weekly_reset_expr = (
        "weekly_reset_at"
        if "weekly_reset_at" in snapshot_columns
        else "NULL AS weekly_reset_at"
    )
    five_hour_window_start_expr = (
        "five_hour_window_start_at"
        if "five_hour_window_start_at" in snapshot_columns
        else "NULL AS five_hour_window_start_at"
    )
    weekly_window_start_expr = (
        "weekly_window_start_at"
        if "weekly_window_start_at" in snapshot_columns
        else "NULL AS weekly_window_start_at"
    )

    conn.execute(
        f"""
        CREATE VIEW {VIEW_NAME} AS
        WITH snapshots AS (
          SELECT
            id AS snapshot_id,
            provider,
            observed_at,
            datetime(observed_at) AS observed_dt,
            five_hour_used_pct,
            weekly_used_pct,
            raw_five_hour_pct,
            raw_weekly_pct,
            raw_mode,
            reset_text,
            {five_hour_reset_expr},
            {weekly_reset_expr},
            {five_hour_window_start_expr},
            {weekly_window_start_expr},
            source,
            dump_file,
            parser_version
          FROM usage_percentage_snapshots
        ),
        usage_windows AS (
          SELECT
            s.snapshot_id,
            s.provider,
            s.observed_at,
            s.observed_dt,
            s.five_hour_used_pct,
            s.weekly_used_pct,
            s.raw_five_hour_pct,
            s.raw_weekly_pct,
            s.raw_mode,
            s.reset_text,
            s.five_hour_reset_at,
            s.weekly_reset_at,
            CASE
              WHEN s.five_hour_window_start_at IS NOT NULL THEN s.five_hour_window_start_at
              WHEN s.five_hour_reset_at IS NOT NULL THEN datetime(s.five_hour_reset_at, '-5 hours')
              ELSE NULL
            END AS exact_five_hour_window_start_at,
            s.five_hour_reset_at AS exact_five_hour_window_end_at,
            CASE
              WHEN s.weekly_window_start_at IS NOT NULL THEN s.weekly_window_start_at
              WHEN s.weekly_reset_at IS NOT NULL THEN datetime(s.weekly_reset_at, '-7 days')
              ELSE NULL
            END AS exact_weekly_window_start_at,
            s.weekly_reset_at AS exact_weekly_window_end_at,
            s.source,
            s.dump_file,
            s.parser_version,

            COALESCE(SUM(
              CASE
                WHEN (
                  CASE
                    WHEN s.five_hour_window_start_at IS NOT NULL THEN s.five_hour_window_start_at
                    WHEN s.five_hour_reset_at IS NOT NULL THEN datetime(s.five_hour_reset_at, '-5 hours')
                    ELSE datetime(s.observed_at, '-5 hours')
                  END
                ) IS NOT NULL
                 AND datetime(u.timestamp) >= datetime(
                  CASE
                    WHEN s.five_hour_window_start_at IS NOT NULL THEN s.five_hour_window_start_at
                    WHEN s.five_hour_reset_at IS NOT NULL THEN datetime(s.five_hour_reset_at, '-5 hours')
                    ELSE datetime(s.observed_at, '-5 hours')
                  END
                 )
                 AND datetime(u.timestamp) <= datetime(
                  CASE
                    WHEN s.five_hour_reset_at IS NOT NULL THEN s.five_hour_reset_at
                    ELSE s.observed_at
                  END
                 )
                THEN u.full_total_tokens
                ELSE 0
              END
            ), 0) AS toktok_last_5h,

            COALESCE(SUM(
              CASE
                WHEN (
                  CASE
                    WHEN s.weekly_window_start_at IS NOT NULL THEN s.weekly_window_start_at
                    WHEN s.weekly_reset_at IS NOT NULL THEN datetime(s.weekly_reset_at, '-7 days')
                    ELSE datetime(s.observed_at, '-7 days')
                  END
                ) IS NOT NULL
                 AND datetime(u.timestamp) >= datetime(
                  CASE
                    WHEN s.weekly_window_start_at IS NOT NULL THEN s.weekly_window_start_at
                    WHEN s.weekly_reset_at IS NOT NULL THEN datetime(s.weekly_reset_at, '-7 days')
                    ELSE datetime(s.observed_at, '-7 days')
                  END
                 )
                 AND datetime(u.timestamp) <= datetime(
                  CASE
                    WHEN s.weekly_reset_at IS NOT NULL THEN s.weekly_reset_at
                    ELSE s.observed_at
                  END
                 )
                THEN u.full_total_tokens
                ELSE 0
              END
            ), 0) AS toktok_last_7d,

            COALESCE(SUM(
              CASE
                WHEN date(u.date) = date(s.observed_at)
                 AND datetime(u.timestamp) <= datetime(s.observed_at)
                THEN u.full_total_tokens
                ELSE 0
              END
            ), 0) AS toktok_same_day

          FROM snapshots s
          LEFT JOIN usage_sessions u
            ON lower(u.tool) = lower(s.provider)
          GROUP BY
            s.snapshot_id,
            s.provider,
            s.observed_at,
            s.observed_dt,
            s.five_hour_used_pct,
            s.weekly_used_pct,
            s.raw_five_hour_pct,
            s.raw_weekly_pct,
            s.raw_mode,
            s.reset_text,
            s.five_hour_reset_at,
            s.weekly_reset_at,
            s.five_hour_window_start_at,
            s.weekly_window_start_at,
            s.source,
            s.dump_file,
            s.parser_version
        )
        SELECT
          snapshot_id,
          provider,
          observed_at,
          observed_dt,

          five_hour_used_pct,
          weekly_used_pct,
          raw_five_hour_pct,
          raw_weekly_pct,
          raw_mode,
          reset_text,
          five_hour_reset_at,
          weekly_reset_at,
          exact_five_hour_window_start_at,
          exact_five_hour_window_end_at,
          exact_weekly_window_start_at,
          exact_weekly_window_end_at,
          CASE
            WHEN exact_five_hour_window_start_at IS NOT NULL AND exact_five_hour_window_end_at IS NOT NULL
            THEN 'exact_reset_window'
            ELSE 'rolling_5h_fallback'
          END AS five_hour_window_source,
          CASE
            WHEN exact_weekly_window_start_at IS NOT NULL AND exact_weekly_window_end_at IS NOT NULL
            THEN 'exact_reset_window'
            ELSE 'rolling_7d_fallback'
          END AS weekly_window_source,
          source,
          dump_file,
          parser_version,

          toktok_last_5h,
          toktok_last_7d,
          toktok_same_day,

          CASE
            WHEN five_hour_used_pct IS NOT NULL AND five_hour_used_pct > 0
            THEN CAST(toktok_last_5h / (five_hour_used_pct / 100.0) AS INTEGER)
            ELSE NULL
          END AS estimated_5h_capacity_toktok,

          CASE
            WHEN weekly_used_pct IS NOT NULL AND weekly_used_pct > 0
            THEN CAST(toktok_last_7d / (weekly_used_pct / 100.0) AS INTEGER)
            ELSE NULL
          END AS estimated_weekly_capacity_toktok,

          CASE
            WHEN five_hour_used_pct IS NOT NULL AND five_hour_used_pct > 0
            THEN CAST(toktok_last_5h / five_hour_used_pct AS INTEGER)
            ELSE NULL
          END AS toktok_per_1pct_5h,

          CASE
            WHEN weekly_used_pct IS NOT NULL AND weekly_used_pct > 0
            THEN CAST(toktok_last_7d / weekly_used_pct AS INTEGER)
            ELSE NULL
          END AS toktok_per_1pct_weekly,

          CASE
            WHEN five_hour_used_pct IS NOT NULL
            THEN 100.0 - five_hour_used_pct
            ELSE NULL
          END AS five_hour_remaining_pct,

          CASE
            WHEN weekly_used_pct IS NOT NULL
            THEN 100.0 - weekly_used_pct
            ELSE NULL
          END AS weekly_remaining_pct,

          CASE
            WHEN five_hour_used_pct IS NULL OR five_hour_used_pct <= 0 THEN 'insufficient_pct'
            WHEN toktok_last_5h <= 0 THEN 'insufficient_toktok'
            ELSE 'usable'
          END AS five_hour_estimate_status,

          CASE
            WHEN weekly_used_pct IS NULL OR weekly_used_pct <= 0 THEN 'insufficient_pct'
            WHEN toktok_last_7d <= 0 THEN 'insufficient_toktok'
            ELSE 'usable'
          END AS weekly_estimate_status

        FROM usage_windows
        """
    )

    conn.commit()


def print_preview(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        f"""
        SELECT
          provider,
          observed_at,
          five_hour_used_pct,
          weekly_used_pct,
          printf('%,d', toktok_last_5h) AS toktok_last_5h,
          printf('%,d', toktok_last_7d) AS toktok_last_7d,
          printf('%,d', estimated_5h_capacity_toktok) AS estimated_5h_capacity,
          printf('%,d', estimated_weekly_capacity_toktok) AS estimated_weekly_capacity,
          printf('%,d', toktok_per_1pct_5h) AS toktok_per_1pct_5h,
          printf('%,d', toktok_per_1pct_weekly) AS toktok_per_1pct_weekly,
          five_hour_estimate_status,
          weekly_estimate_status,
          five_hour_window_source,
          weekly_window_source
        FROM {VIEW_NAME}
        ORDER BY observed_at DESC
        LIMIT 20
        """
    ).fetchall()

    headers = [
        "provider",
        "observed_at",
        "5h_%",
        "weekly_%",
        "toktok_5h",
        "toktok_7d",
        "est_5h_cap",
        "est_week_cap",
        "toktok_1pct_5h",
        "toktok_1pct_week",
        "5h_status",
        "week_status",
        "5h_window",
        "week_window",
    ]

    print("\t".join(headers))
    for row in rows:
        print("\t".join("" if value is None else str(value) for value in row))


def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}. Run sync-usage.py first.")

    conn = sqlite3.connect(DB_PATH)

    try:
        ensure_required_tables(conn)
        create_calibration_view(conn)
        print(f"Created SQLite view: {VIEW_NAME}")
        print(f"Database: {DB_PATH}")
        print()
        print_preview(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
