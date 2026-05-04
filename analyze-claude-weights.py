#!/usr/bin/env python3
"""Analyze Claude token-category weights against observed usage percentage changes.

This is an exploratory calibration tool.

Goal:
- Compare changes in Claude local token telemetry against changes in Claude usage percentages.
- Produce a clean delta table that can later feed a regression/weighting model.

Important:
- This script does not yet claim the true provider quota formula.
- Intervals where percentage decreases are marked as reset/rollover candidates.
- Intervals with unchanged percentage are useful for context but not useful for fitting weights.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / "usage.sqlite"
MAX_ALIGNMENT_LAG_MINUTES = 30.0


@dataclass(frozen=True)
class Snapshot:
    id: int
    observed_at: str
    observed_dt: datetime
    five_hour_used_pct: float | None
    weekly_used_pct: float | None


@dataclass(frozen=True)
class TokenTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def main_total(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def full_total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


def parse_dt(value: str) -> datetime:
    raw = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_int(value: int | float | None) -> str:
    if value is None:
        return "n/a"
    return f"{int(value):,}"


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}"



def fmt_dt(value: datetime) -> str:
    return value.strftime("%d-%m-%Y %H:%M")


# --- Table column helpers ---

def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def first_existing_column(columns: set[str], candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def sum_expr(columns: set[str], candidates: list[str], alias: str) -> str:
    column = first_existing_column(columns, candidates)
    if column is None:
        return f"0 AS {alias}"
    return f"COALESCE(SUM({column}), 0) AS {alias}"


def get_snapshots(conn: sqlite3.Connection) -> list[Snapshot]:
    rows = conn.execute(
        """
        SELECT
          id,
          observed_at,
          five_hour_used_pct,
          weekly_used_pct
        FROM usage_percentage_snapshots
        WHERE provider = 'claude'
        ORDER BY datetime(observed_at), id
        """
    ).fetchall()

    snapshots: list[Snapshot] = []
    for row in rows:
        observed_at = str(row["observed_at"])
        snapshots.append(
            Snapshot(
                id=int(row["id"]),
                observed_at=observed_at,
                observed_dt=parse_dt(observed_at),
                five_hour_used_pct=row["five_hour_used_pct"],
                weekly_used_pct=row["weekly_used_pct"],
            )
        )

    return snapshots



def get_token_totals_between(
    conn: sqlite3.Connection,
    start_dt: datetime,
    end_dt: datetime,
) -> TokenTotals:
    """Return Claude token totals for sessions updated inside (start, end].

    The tracker schema has evolved over time. This function accepts multiple
    possible column names and falls back to zero when a token category is not
    present in the local DB.
    """
    columns = table_columns(conn, "usage_sessions")

    timestamp_column = first_existing_column(columns, ["timestamp", "observed_at", "updated_at", "created_at"])
    if timestamp_column is None:
        raise RuntimeError("usage_sessions has no timestamp-like column")

    tool_column = first_existing_column(columns, ["tool", "provider"])
    if tool_column is None:
        raise RuntimeError("usage_sessions has no tool/provider column")

    select_exprs = [
        sum_expr(columns, ["input_tokens", "claude_input_tokens", "input"], "input_tokens"),
        sum_expr(columns, ["output_tokens", "claude_output_tokens", "output"], "output_tokens"),
        sum_expr(
            columns,
            [
                "cache_creation_input_tokens",
                "cache_creation_tokens",
                "cache_write_tokens",
                "cache_w_tokens",
                "cache_w",
                "cache_write",
            ],
            "cache_creation_input_tokens",
        ),
        sum_expr(
            columns,
            [
                "cache_read_input_tokens",
                "cache_read_tokens",
                "cache_r_tokens",
                "cache_r",
                "cache_read",
            ],
            "cache_read_input_tokens",
        ),
    ]

    row = conn.execute(
        f"""
        SELECT
          {', '.join(select_exprs)}
        FROM usage_sessions
        WHERE lower({tool_column}) = 'claude'
          AND datetime({timestamp_column}) > datetime(?)
          AND datetime({timestamp_column}) <= datetime(?)
        """,
        (start_dt.isoformat(), end_dt.isoformat()),
    ).fetchone()

    return TokenTotals(
        input_tokens=int(row["input_tokens"] or 0),
        output_tokens=int(row["output_tokens"] or 0),
        cache_creation_input_tokens=int(row["cache_creation_input_tokens"] or 0),
        cache_read_input_tokens=int(row["cache_read_input_tokens"] or 0),
    )


def single_pct_status(delta_pct: float | None) -> str:
    if delta_pct is None:
        return "missing_pct"
    if delta_pct < 0:
        return "reset_or_rollover"
    if delta_pct == 0:
        return "unchanged_pct"
    return "candidate"


def pct_per_full_token(delta_pct: float | None, full_total: int) -> float | None:
    if delta_pct is None or delta_pct <= 0 or full_total <= 0:
        return None
    return full_total / delta_pct


# --- Helper: nonzero_token_total ---

def nonzero_token_total(totals: TokenTotals) -> bool:
    return totals.full_total > 0


def print_delta_table(conn: sqlite3.Connection, snapshots: list[Snapshot]) -> None:
    headers = [
        "from",
        "to",
        "Δmin",
        "Δ5h%",
        "Δweek%",
        "input",
        "output",
        "cache_w",
        "cache_r",
        "main",
        "full",
        "full/Δ5h%",
        "full/Δweek%",
        "5h_status",
        "week_status",
    ]

    print("\t".join(headers))

    candidates = 0
    reset_candidates = 0
    unchanged = 0

    for previous, current in zip(snapshots, snapshots[1:]):
        delta_minutes = (current.observed_dt - previous.observed_dt).total_seconds() / 60.0

        delta_5h = None
        if previous.five_hour_used_pct is not None and current.five_hour_used_pct is not None:
            delta_5h = current.five_hour_used_pct - previous.five_hour_used_pct

        delta_weekly = None
        if previous.weekly_used_pct is not None and current.weekly_used_pct is not None:
            delta_weekly = current.weekly_used_pct - previous.weekly_used_pct

        totals = get_token_totals_between(conn, previous.observed_dt, current.observed_dt)

        status_5h = single_pct_status(delta_5h)
        status_week = single_pct_status(delta_weekly)

        if status_5h == "candidate" or status_week == "candidate":
            candidates += 1
        if status_5h == "reset_or_rollover" or status_week == "reset_or_rollover":
            reset_candidates += 1
        if status_5h == "unchanged_pct" and status_week == "unchanged_pct":
            unchanged += 1

        row = [
            fmt_dt(previous.observed_dt),
            fmt_dt(current.observed_dt),
            f"{delta_minutes:.1f}",
            fmt_pct(delta_5h),
            fmt_pct(delta_weekly),
            fmt_int(totals.input_tokens),
            fmt_int(totals.output_tokens),
            fmt_int(totals.cache_creation_input_tokens),
            fmt_int(totals.cache_read_input_tokens),
            fmt_int(totals.main_total),
            fmt_int(totals.full_total),
            fmt_int(pct_per_full_token(delta_5h, totals.full_total)),
            fmt_int(pct_per_full_token(delta_weekly, totals.full_total)),
            status_5h,
            status_week,
        ]
        print("\t".join(row))

    print()
    print("Summary")
    print(f"Claude snapshots:          {len(snapshots):,}")
    print(f"Intervals:                 {max(0, len(snapshots) - 1):,}")
    print(f"Candidate intervals:       {candidates:,}")
    print(f"Reset/rollover intervals:  {reset_candidates:,}")
    print(f"Unchanged intervals:       {unchanged:,}")
    print()
    print("Interpretation")
    print("- candidate = that specific quota percentage increased; usable for calibration")
    print("- reset_or_rollover = that specific quota percentage decreased/reset")
    print("- unchanged_pct = that specific quota percentage unchanged")
    print("- full/Δ% is a rough full-token-per-percentage-point ratio")


# --- Print aligned candidate samples ---

def print_aligned_samples(conn: sqlite3.Connection, snapshots: list[Snapshot]) -> None:
    """Print lag-aware calibration samples.

    Browser percentage snapshots and local Claude token snapshots can be slightly
    misaligned. A common pattern is:

    - interval N: token totals increase, usage percentage is unchanged
    - interval N+1: usage percentage increases, token totals are zero

    This section carries forward recent token totals and assigns them to the
    next positive percentage movement, producing a more useful calibration sample.
    """
    print()
    print("Aligned candidate samples")

    headers = [
        "token_from",
        "pct_to",
        "lag_min",
        "Δ5h%",
        "Δweek%",
        "input",
        "output",
        "cache_w",
        "cache_r",
        "main",
        "full",
        "full/Δ5h%",
        "full/Δweek%",
        "5h_status",
        "week_status",
    ]
    print("\t".join(headers))

    pending_start: Snapshot | None = None
    pending_totals = TokenTotals()
    aligned_count = 0

    for previous, current in zip(snapshots, snapshots[1:]):
        totals = get_token_totals_between(conn, previous.observed_dt, current.observed_dt)

        delta_5h = None
        if previous.five_hour_used_pct is not None and current.five_hour_used_pct is not None:
            delta_5h = current.five_hour_used_pct - previous.five_hour_used_pct

        delta_weekly = None
        if previous.weekly_used_pct is not None and current.weekly_used_pct is not None:
            delta_weekly = current.weekly_used_pct - previous.weekly_used_pct

        status_5h = single_pct_status(delta_5h)
        status_week = single_pct_status(delta_weekly)

        if nonzero_token_total(totals):
            if pending_start is None:
                pending_start = previous
            pending_totals = TokenTotals(
                input_tokens=pending_totals.input_tokens + totals.input_tokens,
                output_tokens=pending_totals.output_tokens + totals.output_tokens,
                cache_creation_input_tokens=(
                    pending_totals.cache_creation_input_tokens + totals.cache_creation_input_tokens
                ),
                cache_read_input_tokens=(
                    pending_totals.cache_read_input_tokens + totals.cache_read_input_tokens
                ),
            )

        has_positive_pct = (
            (delta_5h is not None and delta_5h > 0)
            or (delta_weekly is not None and delta_weekly > 0)
        )

        if pending_start is not None and pending_totals.full_total > 0 and has_positive_pct:
            lag_minutes = (current.observed_dt - pending_start.observed_dt).total_seconds() / 60.0

            if lag_minutes > MAX_ALIGNMENT_LAG_MINUTES:
                pending_start = None
                pending_totals = TokenTotals()
                continue

            row = [
                fmt_dt(pending_start.observed_dt),
                fmt_dt(current.observed_dt),
                f"{lag_minutes:.1f}",
                fmt_pct(delta_5h if delta_5h is not None and delta_5h > 0 else None),
                fmt_pct(delta_weekly if delta_weekly is not None and delta_weekly > 0 else None),
                fmt_int(pending_totals.input_tokens),
                fmt_int(pending_totals.output_tokens),
                fmt_int(pending_totals.cache_creation_input_tokens),
                fmt_int(pending_totals.cache_read_input_tokens),
                fmt_int(pending_totals.main_total),
                fmt_int(pending_totals.full_total),
                fmt_int(
                    pct_per_full_token(delta_5h, pending_totals.full_total)
                    if delta_5h is not None and delta_5h > 0
                    else None
                ),
                fmt_int(
                    pct_per_full_token(delta_weekly, pending_totals.full_total)
                    if delta_weekly is not None and delta_weekly > 0
                    else None
                ),
                status_5h,
                status_week,
            ]
            print("\t".join(row))
            aligned_count += 1

            pending_start = None
            pending_totals = TokenTotals()

        # If both windows reset/decrease, discard carried tokens because the
        # attribution boundary is no longer reliable.
        if status_5h == "reset_or_rollover" and status_week == "reset_or_rollover":
            pending_start = None
            pending_totals = TokenTotals()

        if pending_start is not None:
            pending_lag_minutes = (current.observed_dt - pending_start.observed_dt).total_seconds() / 60.0
            if pending_lag_minutes > MAX_ALIGNMENT_LAG_MINUTES:
                pending_start = None
                pending_totals = TokenTotals()

    if aligned_count == 0:
        print("No aligned samples found yet.")

    print()
    print("Aligned sample interpretation")
    print("- token_from = first interval where carried token activity started")
    print("- pct_to = snapshot where quota percentage movement was observed")
    print("- lag_min = elapsed time between token activity and observed percentage movement")
    print(f"- samples are discarded when lag_min exceeds {MAX_ALIGNMENT_LAG_MINUTES:.0f} minutes")
    print("- This helps when local tokens are logged before the provider usage page updates")


def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}. Run sync first.")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    print("usage_sessions columns:")
    print(", ".join(sorted(table_columns(conn, "usage_sessions"))))
    print()

    try:
        snapshots = get_snapshots(conn)
        if len(snapshots) < 2:
            raise SystemExit("Need at least two Claude percentage snapshots.")

        print_delta_table(conn, snapshots)
        print_aligned_samples(conn, snapshots)
    finally:
        conn.close()


if __name__ == "__main__":
    main()