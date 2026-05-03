#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / "usage.sqlite"

def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}. Run sync-usage.py first.")

    conn = sqlite3.connect(DB_PATH)

    try:
        conn.execute("DROP VIEW IF EXISTS quota_forecast")

        conn.execute("""
        CREATE VIEW quota_forecast AS
        WITH latest AS (
          SELECT *
          FROM calibration_estimates
          WHERE observed_at IN (
            SELECT MAX(observed_at)
            FROM calibration_estimates
            GROUP BY provider
          )
        ),
        normalized AS (
          SELECT
            provider,
            observed_at,
            five_hour_used_pct,
            weekly_used_pct,

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

            toktok_last_5h,
            toktok_last_7d,
            estimated_5h_capacity_toktok,
            estimated_weekly_capacity_toktok,
            five_hour_estimate_status,
            weekly_estimate_status
          FROM latest
        ),
        rates AS (
          SELECT
            provider,
            observed_at,
            five_hour_used_pct,
            weekly_used_pct,
            five_hour_remaining_pct,
            weekly_remaining_pct,
            toktok_last_5h,
            toktok_last_7d,
            estimated_5h_capacity_toktok,
            estimated_weekly_capacity_toktok,
            five_hour_estimate_status,
            weekly_estimate_status,

            CASE
              WHEN toktok_last_5h > 0
              THEN toktok_last_5h / 5.0
              ELSE NULL
            END AS avg_toktok_per_hour_5h,

            CASE
              WHEN toktok_last_7d > 0
              THEN toktok_last_7d / 168.0
              ELSE NULL
            END AS avg_toktok_per_hour_7d

          FROM normalized
        )
        SELECT
          provider,
          observed_at,

          five_hour_used_pct,
          five_hour_remaining_pct,
          weekly_used_pct,
          weekly_remaining_pct,

          toktok_last_5h,
          toktok_last_7d,

          estimated_5h_capacity_toktok,
          estimated_weekly_capacity_toktok,

          avg_toktok_per_hour_5h,
          avg_toktok_per_hour_7d,

          CASE
            WHEN five_hour_estimate_status = 'usable'
             AND avg_toktok_per_hour_5h > 0
             AND five_hour_remaining_pct IS NOT NULL
            THEN
              ((estimated_5h_capacity_toktok * (five_hour_remaining_pct / 100.0)) / avg_toktok_per_hour_5h)
            ELSE NULL
          END AS estimated_hours_to_5h_limit,

          CASE
            WHEN weekly_estimate_status = 'usable'
             AND avg_toktok_per_hour_7d > 0
             AND weekly_remaining_pct IS NOT NULL
            THEN
              ((estimated_weekly_capacity_toktok * (weekly_remaining_pct / 100.0)) / avg_toktok_per_hour_7d)
            ELSE NULL
          END AS estimated_hours_to_weekly_limit,

          CASE
            WHEN five_hour_estimate_status != 'usable' THEN 'insufficient_data'
            WHEN five_hour_remaining_pct <= 5 THEN 'critical'
            WHEN five_hour_remaining_pct <= 15 THEN 'warning'
            ELSE 'ok'
          END AS five_hour_risk,

          CASE
            WHEN weekly_estimate_status != 'usable' THEN 'insufficient_data'
            WHEN weekly_remaining_pct <= 5 THEN 'critical'
            WHEN weekly_remaining_pct <= 15 THEN 'warning'
            ELSE 'ok'
          END AS weekly_risk

        FROM rates
        """)

        conn.commit()

        print("Created SQLite view: quota_forecast")
        print()

        rows = conn.execute("""
        SELECT
          provider,
          observed_at,
          five_hour_used_pct,
          weekly_used_pct,
          printf('%,d', estimated_5h_capacity_toktok),
          printf('%,d', estimated_weekly_capacity_toktok),
          printf('%.2f', estimated_hours_to_5h_limit),
          printf('%.2f', estimated_hours_to_weekly_limit),
          five_hour_risk,
          weekly_risk
        FROM quota_forecast
        ORDER BY provider
        """).fetchall()

        print("provider\tobserved_at\t5h_used\tweekly_used\test_5h_cap\test_week_cap\thrs_to_5h\thrs_to_week\t5h_risk\tweek_risk")
        for row in rows:
            print("\t".join("" if value is None else str(value) for value in row))

    finally:
        conn.close()

if __name__ == "__main__":
    main()
