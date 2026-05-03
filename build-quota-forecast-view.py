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
            weekly_estimate_status,
            exact_five_hour_window_end_at,
            exact_weekly_window_end_at,

            CASE
              WHEN exact_five_hour_window_end_at IS NOT NULL
              THEN (julianday(exact_five_hour_window_end_at) - julianday(observed_at)) * 24.0
              ELSE NULL
            END AS hours_until_5h_reset,

            CASE
              WHEN exact_weekly_window_end_at IS NOT NULL
              THEN (julianday(exact_weekly_window_end_at) - julianday(observed_at)) * 24.0
              ELSE NULL
            END AS hours_until_weekly_reset
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
            exact_five_hour_window_end_at,
            exact_weekly_window_end_at,
            hours_until_5h_reset,
            hours_until_weekly_reset,

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

          exact_five_hour_window_end_at,
          exact_weekly_window_end_at,
          hours_until_5h_reset,
          hours_until_weekly_reset,

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
            WHEN hours_until_5h_reset IS NOT NULL
             AND hours_until_5h_reset >= 0
            THEN hours_until_5h_reset
            ELSE NULL
          END AS actual_hours_until_5h_reset,

          CASE
            WHEN hours_until_weekly_reset IS NOT NULL
             AND hours_until_weekly_reset >= 0
            THEN hours_until_weekly_reset
            ELSE NULL
          END AS actual_hours_until_weekly_reset,

          CASE
            WHEN hours_until_5h_reset IS NULL THEN 'unknown_reset'
            WHEN hours_until_5h_reset < 0 THEN 'reset_time_passed'
            ELSE 'known_reset'
          END AS five_hour_reset_status,

          CASE
            WHEN hours_until_weekly_reset IS NULL THEN 'unknown_reset'
            WHEN hours_until_weekly_reset < 0 THEN 'reset_time_passed'
            ELSE 'known_reset'
          END AS weekly_reset_status,

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
          CASE
            WHEN estimated_hours_to_5h_limit IS NULL THEN 'n/a'
            ELSE printf('%.2f', estimated_hours_to_5h_limit)
          END,
          CASE
            WHEN estimated_hours_to_weekly_limit IS NULL THEN 'n/a'
            ELSE printf('%.2f', estimated_hours_to_weekly_limit)
          END,
          CASE
            WHEN actual_hours_until_5h_reset IS NULL THEN 'n/a'
            ELSE printf('%.2f', actual_hours_until_5h_reset)
          END,
          CASE
            WHEN actual_hours_until_weekly_reset IS NULL THEN 'n/a'
            ELSE printf('%.2f', actual_hours_until_weekly_reset)
          END,
          five_hour_risk,
          weekly_risk,
          five_hour_reset_status,
          weekly_reset_status
        FROM quota_forecast
        ORDER BY provider
        """).fetchall()

        print("provider\tobserved_at\t5h_used\tweekly_used\test_5h_cap\test_week_cap\thrs_to_5h_limit\thrs_to_week_limit\thrs_to_5h_reset\thrs_to_week_reset\t5h_risk\tweek_risk\t5h_reset\tweek_reset")
        for row in rows:
            print("\t".join("" if value is None else str(value) for value in row))

    finally:
        conn.close()

if __name__ == "__main__":
    main()
