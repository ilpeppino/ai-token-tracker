#!/usr/bin/env python3
import sqlite3
from pathlib import Path

BASE = Path("/Volumes/DevSSD/projects/ai-token-tracker")
DB_PATH = BASE / "usage.sqlite"

def main():
    conn = sqlite3.connect(DB_PATH)

    conn.execute("DROP VIEW IF EXISTS calibration_estimates")

    conn.execute("""
    CREATE VIEW calibration_estimates AS
    WITH snapshots AS (
      SELECT
        id AS snapshot_id,
        provider,
        observed_at,
        datetime(observed_at) AS observed_dt,
        five_hour_used_pct,
        weekly_used_pct
      FROM usage_percentage_snapshots
    ),
    usage_windows AS (
      SELECT
        s.snapshot_id,
        s.provider,
        s.observed_at,
        s.five_hour_used_pct,
        s.weekly_used_pct,

        COALESCE(SUM(
          CASE
            WHEN datetime(u.timestamp) >= datetime(s.observed_at, '-5 hours')
             AND datetime(u.timestamp) <= datetime(s.observed_at)
            THEN u.full_total_tokens
            ELSE 0
          END
        ), 0) AS toktok_last_5h,

        COALESCE(SUM(
          CASE
            WHEN date(u.date) >= date(s.observed_at, '-7 days')
             AND datetime(u.timestamp) <= datetime(s.observed_at)
            THEN u.full_total_tokens
            ELSE 0
          END
        ), 0) AS toktok_last_7d

      FROM snapshots s
      LEFT JOIN usage_sessions u
        ON lower(u.tool) = lower(s.provider)
      GROUP BY
        s.snapshot_id,
        s.provider,
        s.observed_at,
        s.five_hour_used_pct,
        s.weekly_used_pct
    )
    SELECT
      snapshot_id,
      provider,
      observed_at,

      five_hour_used_pct,
      weekly_used_pct,

      toktok_last_5h,
      toktok_last_7d,

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
      END AS toktok_per_1pct_weekly

    FROM usage_windows
    """)

    conn.commit()

    print("Created SQLite view: calibration_estimates")
    print()

    rows = conn.execute("""
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
      printf('%,d', toktok_per_1pct_weekly) AS toktok_per_1pct_weekly
    FROM calibration_estimates
    ORDER BY observed_at DESC
    LIMIT 20
    """).fetchall()

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
    ]

    print("\t".join(headers))
    for row in rows:
        print("\t".join("" if v is None else str(v) for v in row))

    conn.close()

if __name__ == "__main__":
    main()
