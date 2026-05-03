#!/usr/bin/env python3
import sqlite3
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / "usage.sqlite"

st.set_page_config(
    page_title="AI Token Usage Dashboard",
    page_icon="📊",
    layout="wide",
)

st.title("AI Token Usage Dashboard")

if not DB_PATH.exists():
    st.error(f"Database not found: {DB_PATH}")
    st.stop()

conn = sqlite3.connect(DB_PATH)

calibration_df = pd.DataFrame()
quota_forecast_df = pd.DataFrame()

calibration_view_exists = conn.execute(
    "SELECT COUNT(*) FROM sqlite_master WHERE type='view' AND name='calibration_estimates'"
).fetchone()[0] > 0

quota_forecast_view_exists = conn.execute(
    "SELECT COUNT(*) FROM sqlite_master WHERE type='view' AND name='quota_forecast'"
).fetchone()[0] > 0

if calibration_view_exists:
    calibration_df = pd.read_sql_query(
        """
        SELECT
          snapshot_id,
          provider,
          observed_at,
          five_hour_used_pct,
          weekly_used_pct,
          five_hour_remaining_pct,
          weekly_remaining_pct,
          toktok_last_5h,
          toktok_last_7d,
          toktok_same_day,
          estimated_5h_capacity_toktok,
          estimated_weekly_capacity_toktok,
          toktok_per_1pct_5h,
          toktok_per_1pct_weekly,
          five_hour_estimate_status,
          weekly_estimate_status,
          raw_mode,
          reset_text,
          source,
          parser_version
        FROM calibration_estimates
        ORDER BY observed_at DESC
        """,
        conn,
    )

if quota_forecast_view_exists:
    quota_forecast_df = pd.read_sql_query(
        """
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
          estimated_hours_to_5h_limit,
          estimated_hours_to_weekly_limit,
          actual_hours_until_5h_reset,
          actual_hours_until_weekly_reset,
          five_hour_reset_status,
          weekly_reset_status,
          five_hour_risk,
          weekly_risk
        FROM quota_forecast
        ORDER BY provider
        """,
        conn,
    )

df = pd.read_sql_query(
    """
    SELECT
      tool,
      session_id,
      date,
      timestamp,
      project,
      cwd,
      model,
      reasoning_effort,
      input_tokens,
      output_tokens,
      cache_read_tokens,
      cache_write_tokens,
      main_total_tokens,
      full_total_tokens,
      reported_total_tokens,
      cost_usd,
      live
    FROM usage_sessions
    ORDER BY date ASC, timestamp ASC
    """,
    conn,
)

conn.close()

if df.empty:
    st.warning("No usage data found yet. Run sync-usage.py first.")
    st.stop()

df["date"] = pd.to_datetime(df["date"])
if not calibration_df.empty:
    calibration_df["observed_at"] = pd.to_datetime(calibration_df["observed_at"])

if not quota_forecast_df.empty:
    quota_forecast_df["observed_at"] = pd.to_datetime(quota_forecast_df["observed_at"])



st.sidebar.header("Budget / Forecast")

monthly_claude_budget = st.sidebar.number_input(
    "Claude monthly budget USD",
    min_value=0.0,
    value=100.0,
    step=10.0,
)

monthly_full_token_target = st.sidebar.number_input(
    "Monthly full-Toktok target",
    min_value=0,
    value=1_200_000_000,
    step=10_000_000,
)


st.sidebar.header("Filters")

all_tools = sorted(df["tool"].dropna().unique().tolist())
selected_tools = st.sidebar.multiselect(
    "Tools",
    all_tools,
    default=all_tools,
)

all_projects = sorted(df["project"].dropna().unique().tolist())
selected_projects = st.sidebar.multiselect(
    "Projects",
    all_projects,
    default=all_projects,
)

min_date = df["date"].min().date()
max_date = df["date"].max().date()

selected_range = st.sidebar.date_input(
    "Date range",
    value=(min_date, max_date),
    min_value=min_date,
    max_value=max_date,
)

if isinstance(selected_range, tuple) and len(selected_range) == 2:
    start_date, end_date = selected_range
else:
    start_date, end_date = min_date, max_date

df = df[
    (df["tool"].isin(selected_tools))
    & (df["project"].isin(selected_projects))
    & (df["date"].dt.date >= start_date)
    & (df["date"].dt.date <= end_date)
].copy()

if df.empty:
    st.warning("No data matches the selected filters.")
    st.stop()


daily = (
    df.groupby(["date", "tool"], as_index=False)
    .agg(
        sessions=("session_id", "count"),
        main_total_tokens=("main_total_tokens", "sum"),
        full_total_tokens=("full_total_tokens", "sum"),
        cost_usd=("cost_usd", "sum"),
    )
)

combined_daily = (
    df.groupby("date", as_index=False)
    .agg(
        sessions=("session_id", "count"),
        main_total_tokens=("main_total_tokens", "sum"),
        full_total_tokens=("full_total_tokens", "sum"),
        cost_usd=("cost_usd", "sum"),
    )
)

combined_daily = combined_daily.sort_values("date")
combined_daily["full_tokens_7d_avg"] = combined_daily["full_total_tokens"].rolling(7, min_periods=1).mean()
combined_daily["main_tokens_7d_avg"] = combined_daily["main_total_tokens"].rolling(7, min_periods=1).mean()

today = pd.Timestamp.today().normalize()
today_df = df[df["date"] == today]

today_full = int(today_df["full_total_tokens"].sum()) if not today_df.empty else 0
today_main = int(today_df["main_total_tokens"].sum()) if not today_df.empty else 0
today_cost = float(today_df["cost_usd"].sum()) if not today_df.empty else 0.0
today_sessions = int(today_df["session_id"].count()) if not today_df.empty else 0

last_7 = combined_daily[combined_daily["date"] >= today - pd.Timedelta(days=6)]
avg_7_full = int(last_7["full_total_tokens"].mean()) if not last_7.empty else 0

month_df = combined_daily[
    (combined_daily["date"].dt.year == today.year)
    & (combined_daily["date"].dt.month == today.month)
]
month_full = int(month_df["full_total_tokens"].sum()) if not month_df.empty else 0
month_cost = float(month_df["cost_usd"].sum()) if not month_df.empty else 0.0

days_in_month = today.days_in_month
day_of_month = today.day

projected_month_full = int((month_full / day_of_month) * days_in_month) if day_of_month else 0
projected_month_cost = float((month_cost / day_of_month) * days_in_month) if day_of_month else 0.0

highest_day = int(combined_daily["full_total_tokens"].max()) if not combined_daily.empty else 0
today_vs_7d = (today_full / avg_7_full) if avg_7_full else 0

latest_calibration = pd.DataFrame()
if not calibration_df.empty:
    latest_calibration = calibration_df.sort_values("observed_at").groupby("provider", as_index=False).tail(1)

latest_five_hour = None
latest_weekly = None
if not latest_calibration.empty:
    usable_weekly = latest_calibration[latest_calibration["weekly_estimate_status"] == "usable"]
    usable_five_hour = latest_calibration[latest_calibration["five_hour_estimate_status"] == "usable"]
    if not usable_five_hour.empty:
        latest_five_hour = usable_five_hour.sort_values("observed_at").iloc[-1]
    if not usable_weekly.empty:
        latest_weekly = usable_weekly.sort_values("observed_at").iloc[-1]

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Today full Toktok", f"{today_full:,}")
col2.metric("7-day avg full Toktok", f"{avg_7_full:,}")
col3.metric("Month-to-date full Toktok", f"{month_full:,}")
col4.metric("Projected month full Toktok", f"{projected_month_full:,}")
col5.metric("Projected Claude cost", f"${projected_month_cost:,.2f}")

col6, col7, col8, col9, col10 = st.columns(5)
col6.metric("Today main tokens", f"{today_main:,}")
col7.metric("Today sessions", f"{today_sessions:,}")
col8.metric("Highest day full Toktok", f"{highest_day:,}")
col9.metric("Today vs 7d avg", f"{today_vs_7d:.2f}×")
col10.metric("Month cost", f"${month_cost:,.2f}")

if monthly_claude_budget > 0 and projected_month_cost > monthly_claude_budget:
    st.warning(
        f"Projected Claude cost is ${projected_month_cost:,.2f}, "
        f"above your ${monthly_claude_budget:,.2f} monthly budget."
    )

if monthly_full_token_target > 0 and projected_month_full > monthly_full_token_target:
    st.warning(
        f"Projected full-Toktok usage is {projected_month_full:,}, "
        f"above your {monthly_full_token_target:,} monthly target."
    )

if today_vs_7d >= 1.5:
    st.info(f"Today is {today_vs_7d:.2f}× above your 7-day average.")

st.divider()

st.subheader("Quota depletion forecast")

if quota_forecast_df.empty:
    st.info(
        "No quota forecast yet. Run browser usage scraping, sync-usage-percentages.py, "
        "build-calibration-view.py, and build-quota-forecast-view.py."
    )
else:
    forecast_cards = quota_forecast_df.copy()

    for _, row in forecast_cards.iterrows():
        provider = row["provider"]

        st.markdown(f"#### {provider.capitalize()}")

        f1, f2, f3, f4 = st.columns(4)

        five_hour_used = row["five_hour_used_pct"]
        weekly_used = row["weekly_used_pct"]

        hrs_5h_limit = row["estimated_hours_to_5h_limit"]
        hrs_week_limit = row["estimated_hours_to_weekly_limit"]
        hrs_5h_reset = row["actual_hours_until_5h_reset"]
        hrs_week_reset = row["actual_hours_until_weekly_reset"]

        f1.metric(
            "5h used",
            "n/a" if pd.isna(five_hour_used) else f"{five_hour_used:.0f}%",
        )
        f2.metric(
            "Weekly used",
            "n/a" if pd.isna(weekly_used) else f"{weekly_used:.0f}%",
        )
        f3.metric(
            "Est. time to 5h limit",
            "n/a" if pd.isna(hrs_5h_limit) else f"{hrs_5h_limit:.1f}h",
        )
        f4.metric(
            "Est. time to weekly limit",
            "n/a" if pd.isna(hrs_week_limit) else f"{hrs_week_limit:.1f}h",
        )

        r1, r2, r3, r4 = st.columns(4)
        r1.metric(
            "Actual 5h reset countdown",
            "n/a" if pd.isna(hrs_5h_reset) else f"{hrs_5h_reset:.1f}h",
        )
        r2.metric(
            "Actual weekly reset countdown",
            "n/a" if pd.isna(hrs_week_reset) else f"{hrs_week_reset:.1f}h",
        )
        r3.metric(
            "5h reset status",
            str(row.get("five_hour_reset_status", "unknown")),
        )
        r4.metric(
            "Weekly reset status",
            str(row.get("weekly_reset_status", "unknown")),
        )

        risk_5h = row["five_hour_risk"]
        risk_week = row["weekly_risk"]

        if risk_5h == "critical" or risk_week == "critical":
            st.error(f"{provider.capitalize()} quota risk is critical.")
        elif risk_5h == "warning" or risk_week == "warning":
            st.warning(f"{provider.capitalize()} quota risk is elevated.")
        elif risk_5h == "insufficient_data" or risk_week == "insufficient_data":
            st.info(f"{provider.capitalize()} forecast needs more calibration data.")

        if not pd.isna(hrs_week_limit) and not pd.isna(hrs_week_reset):
            if hrs_week_limit < hrs_week_reset:
                st.warning(
                    f"{provider.capitalize()} may hit the weekly limit in ~{hrs_week_limit:.1f}h, "
                    f"before the reset in ~{hrs_week_reset:.1f}h."
                )
            else:
                st.success(
                    f"{provider.capitalize()} weekly reset is expected before the projected limit."
                )

    forecast_table = quota_forecast_df.copy()
    forecast_table["observed_at"] = forecast_table["observed_at"].dt.strftime("%Y-%m-%d %H:%M:%S")
    st.dataframe(forecast_table, width="stretch", hide_index=True)

    st.caption(
        "Forecast uses empirical Toktok calibration. Time-to-limit is estimated from local burn rate. "
        "Time-to-reset comes from vendor page reset text when available. This is not an official vendor quota API."
    )


st.divider()

st.subheader("Toktok ↔ Vendor usage calibration")

if calibration_df.empty:
    st.info(
        "No calibration data yet. Run browser usage scraping, sync-usage-percentages.py, "
        "and build-calibration-view.py to populate this section."
    )
else:
    latest_display = latest_calibration.copy()
    latest_display["observed_at"] = latest_display["observed_at"].dt.strftime("%Y-%m-%d %H:%M:%S")

    c1, c2, c3, c4 = st.columns(4)

    if latest_five_hour is not None:
        c1.metric(
            f"{latest_five_hour['provider']} 5h used",
            f"{latest_five_hour['five_hour_used_pct']:.0f}%",
        )
        c2.metric(
            "Estimated 5h capacity",
            f"{int(latest_five_hour['estimated_5h_capacity_toktok']):,} Toktok",
        )
    else:
        c1.metric("5h used", "n/a")
        c2.metric("Estimated 5h capacity", "n/a")

    if latest_weekly is not None:
        c3.metric(
            f"{latest_weekly['provider']} weekly used",
            f"{latest_weekly['weekly_used_pct']:.0f}%",
        )
        c4.metric(
            "Estimated weekly capacity",
            f"{int(latest_weekly['estimated_weekly_capacity_toktok']):,} Toktok",
        )
    else:
        c3.metric("Weekly used", "n/a")
        c4.metric("Estimated weekly capacity", "n/a")

    calibration_plot_df = calibration_df.copy()
    calibration_plot_df = calibration_plot_df[
        calibration_plot_df["weekly_estimate_status"] == "usable"
    ].copy()

    if not calibration_plot_df.empty:
        fig_cal = px.scatter(
            calibration_plot_df,
            x="toktok_last_7d",
            y="weekly_used_pct",
            color="provider",
            size="estimated_weekly_capacity_toktok",
            hover_data=[
                "observed_at",
                "toktok_per_1pct_weekly",
                "estimated_weekly_capacity_toktok",
                "reset_text",
            ],
            labels={
                "toktok_last_7d": "Toktok in last 7 days",
                "weekly_used_pct": "Vendor weekly usage %",
                "provider": "Provider",
            },
            title="Weekly Toktok vs vendor usage %",
        )
        st.plotly_chart(fig_cal, width="stretch")
    else:
        st.info("No usable weekly calibration estimates yet.")

    calibration_table = calibration_df.sort_values("observed_at", ascending=False).copy()
    calibration_table["observed_at"] = calibration_table["observed_at"].dt.strftime("%Y-%m-%d %H:%M:%S")
    calibration_table = calibration_table[
        [
            "observed_at",
            "provider",
            "five_hour_used_pct",
            "weekly_used_pct",
            "toktok_last_5h",
            "toktok_last_7d",
            "estimated_5h_capacity_toktok",
            "estimated_weekly_capacity_toktok",
            "toktok_per_1pct_5h",
            "toktok_per_1pct_weekly",
            "five_hour_estimate_status",
            "weekly_estimate_status",
            "reset_text",
        ]
    ]
    st.dataframe(calibration_table, width="stretch", hide_index=True)

    st.caption(
        "Calibration values are empirical estimates. Toktok is a local measurement unit, "
        "not an official vendor token. 5h estimates use the previous 5 local hours; weekly "
        "estimates use the previous 7 local days until exact vendor reset windows are modeled."
    )

st.divider()

st.subheader("Daily full Toktok usage by tool")
fig = px.line(
    daily,
    x="date",
    y="full_total_tokens",
    color="tool",
    markers=True,
    labels={
        "date": "Date",
        "full_total_tokens": "Full Toktok",
        "tool": "Tool",
    },
)
st.plotly_chart(fig, width="stretch")

st.subheader("Combined daily usage with 7-day average")

fig2 = px.bar(
    combined_daily,
    x="date",
    y="full_total_tokens",
    labels={
        "date": "Date",
        "full_total_tokens": "Full Toktok",
    },
)

fig2.add_scatter(
    x=combined_daily["date"],
    y=combined_daily["full_tokens_7d_avg"],
    mode="lines+markers",
    name="7-day avg full Toktok",
)

st.plotly_chart(fig2, width="stretch")

st.subheader("Claude estimated cost")
cost_daily = combined_daily[combined_daily["cost_usd"] > 0]
if cost_daily.empty:
    st.info("No cost data yet.")
else:
    fig3 = px.line(
        cost_daily,
        x="date",
        y="cost_usd",
        markers=True,
        labels={
            "date": "Date",
            "cost_usd": "Cost USD",
        },
    )
    st.plotly_chart(fig3, width="stretch")

st.subheader("Project breakdown")
project_daily = (
    df.groupby(["project", "tool"], as_index=False)
    .agg(full_total_tokens=("full_total_tokens", "sum"))
    .sort_values("full_total_tokens", ascending=False)
)
fig4 = px.bar(
    project_daily,
    x="project",
    y="full_total_tokens",
    color="tool",
    labels={
        "project": "Project",
        "full_total_tokens": "Full Toktok",
        "tool": "Tool",
    },
)
st.plotly_chart(fig4, width="stretch")

st.subheader("Sessions")
display_df = df.sort_values("timestamp", ascending=False).copy()
display_df["date"] = display_df["date"].dt.strftime("%Y-%m-%d")
display_df["cost_usd"] = display_df["cost_usd"].map(lambda x: f"${x:.4f}")
display_df = display_df[
    [
        "date",
        "tool",
        "project",
        "model",
        "reasoning_effort",
        "session_id",
        "main_total_tokens",
        "full_total_tokens",
        "cost_usd",
        "live",
    ]
]

st.dataframe(display_df, width="stretch", hide_index=True)

st.caption(
    "MAIN Toktok = Claude input + output; Codex reported total. "
    "FULL Toktok = Claude input + output + cache read + cache write; Codex reported total."
)


st.subheader("Month-to-date by tool")

month_tool = df[
    (df["date"].dt.year == today.year)
    & (df["date"].dt.month == today.month)
].groupby("tool", as_index=False).agg(
    sessions=("session_id", "count"),
    main_total_tokens=("main_total_tokens", "sum"),
    full_total_tokens=("full_total_tokens", "sum"),
    cost_usd=("cost_usd", "sum"),
)

if month_tool.empty:
    st.info("No month-to-date data for the selected filters.")
else:
    fig5 = px.bar(
        month_tool,
        x="tool",
        y="full_total_tokens",
        labels={
            "tool": "Tool",
            "full_total_tokens": "Month-to-date full Toktok",
        },
    )
    st.plotly_chart(fig5, width="stretch")

    st.dataframe(month_tool, width="stretch", hide_index=True)

st.subheader("Top sessions by full Toktok")

top_sessions = df.sort_values("full_total_tokens", ascending=False).head(20).copy()
top_sessions["date"] = top_sessions["date"].dt.strftime("%Y-%m-%d")
top_sessions["cost_usd"] = top_sessions["cost_usd"].map(lambda x: f"${x:.4f}")

st.dataframe(
    top_sessions[
        [
            "date",
            "tool",
            "project",
            "model",
            "session_id",
            "main_total_tokens",
            "full_total_tokens",
            "cost_usd",
        ]
    ],
    width="stretch",
    hide_index=True,
)


st.subheader("Monthly projection")

projection_df = pd.DataFrame(
    [
        {
            "metric": "Month-to-date full tokens",
            "value": month_full,
        },
        {
            "metric": "Projected month-end full tokens",
            "value": projected_month_full,
        },
        {
            "metric": "Monthly full-Toktok target",
            "value": monthly_full_token_target,
        },
    ]
)

fig6 = px.bar(
    projection_df,
    x="metric",
    y="value",
    labels={
        "metric": "Metric",
        "value": "Full Toktok",
    },
)
st.plotly_chart(fig6, width="stretch")

cost_projection_df = pd.DataFrame(
    [
        {
            "metric": "Month-to-date Claude cost",
            "value": month_cost,
        },
        {
            "metric": "Projected month-end Claude cost",
            "value": projected_month_cost,
        },
        {
            "metric": "Claude monthly budget",
            "value": monthly_claude_budget,
        },
    ]
)

fig7 = px.bar(
    cost_projection_df,
    x="metric",
    y="value",
    labels={
        "metric": "Metric",
        "value": "USD",
    },
)
st.plotly_chart(fig7, width="stretch")
