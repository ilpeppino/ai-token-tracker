#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-daily}"

if [[ "$MODE" != "today" && "$MODE" != "daily" && "$MODE" != "current" ]]; then
  echo "Usage: $0 [today|daily|current]"
  exit 1
fi

python3 - "$MODE" << 'PYEOF'
import json
import os
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone, date
from collections import defaultdict

MODE = sys.argv[1]

HOME = Path.home()

CLAUDE_DIR = HOME / ".claude"
CLAUDE_TOKEN_LOG = CLAUDE_DIR / "token-usage.jsonl"

CODEX_DB = HOME / ".codex" / "state_5.sqlite"
CODEX_THREAD_ID = os.environ.get("CODEX_THREAD_ID", "")

CLAUDE_PRICE = {
    "input": 3.00,
    "output": 15.00,
    "cache_read": 0.30,
    "cache_write": 3.75,
}

def fmt(n):
    if n is None:
        return "-"
    return f"{int(n):,}"

def short_project(cwd):
    if not cwd:
        return "?"
    return cwd.rstrip("/").split("/")[-1] or "?"

def claude_cost(inp, out, cr, cw):
    return (
        inp * CLAUDE_PRICE["input"] / 1e6
        + out * CLAUDE_PRICE["output"] / 1e6
        + cr * CLAUDE_PRICE["cache_read"] / 1e6
        + cw * CLAUDE_PRICE["cache_write"] / 1e6
    )

def claude_full_tokens(inp, out, cr, cw):
    return inp + out + cr + cw

def read_claude_transcript_tokens(path):
    seen = set()
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }

    try:
        with open(path) as f:
            for line in f:
                try:
                    e = json.loads(line.strip())
                    uid = e.get("uuid")
                    if uid and uid in seen:
                        continue

                    msg = e.get("message")
                    if not isinstance(msg, dict):
                        continue

                    usage = msg.get("usage")
                    if not usage:
                        continue

                    if uid:
                        seen.add(uid)

                    for k in totals:
                        totals[k] += usage.get(k, 0)
                except Exception:
                    continue
    except Exception:
        pass

    return totals

def load_claude_sessions():
    sessions = {}

    if CLAUDE_TOKEN_LOG.exists():
        with open(CLAUDE_TOKEN_LOG) as f:
            for line in f:
                try:
                    e = json.loads(line.strip())
                    sid = e["session_id"]
                    e["tool"] = "claude"
                    sessions[sid] = e
                except Exception:
                    continue

    sessions_dir = CLAUDE_DIR / "sessions"
    projects_dir = CLAUDE_DIR / "projects"

    if sessions_dir.exists() and projects_dir.exists():
        for sf in sessions_dir.glob("*.json"):
            try:
                meta = json.loads(sf.read_text())
                pid = meta.get("pid")
                sid = meta.get("sessionId", "")
                cwd = meta.get("cwd", "")

                if not pid or not sid:
                    continue

                try:
                    os.kill(pid, 0)
                    alive = True
                except OSError:
                    alive = False

                if not alive:
                    continue

                transcript_path = ""
                for pd in projects_dir.iterdir():
                    c = pd / f"{sid}.jsonl"
                    if c.exists():
                        transcript_path = str(c)
                        break

                if not transcript_path:
                    continue

                tokens = read_claude_transcript_tokens(transcript_path)
                sessions[sid] = {
                    "tool": "claude",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "session_id": sid,
                    "cwd": cwd,
                    "transcript_path": transcript_path,
                    "live": True,
                    **tokens,
                }
            except Exception:
                continue

    return list(sessions.values())

def load_codex_sessions():
    if not CODEX_DB.exists():
        return []

    rows = []
    try:
        conn = sqlite3.connect(str(CODEX_DB))
        conn.row_factory = sqlite3.Row

        q = """
        select
          id,
          cwd,
          model,
          reasoning_effort,
          tokens_used,
          created_at,
          updated_at
        from threads
        order by updated_at desc
        """

        for r in conn.execute(q):
            created = datetime.fromtimestamp(r["created_at"], tz=timezone.utc).isoformat()
            updated = datetime.fromtimestamp(r["updated_at"], tz=timezone.utc).isoformat()

            rows.append({
                "tool": "codex",
                "timestamp": updated,
                "created_timestamp": created,
                "session_id": r["id"],
                "cwd": r["cwd"],
                "model": r["model"] or "",
                "reasoning_effort": r["reasoning_effort"] or "",
                "total_tokens": r["tokens_used"] or 0,
                "live": bool(CODEX_THREAD_ID and r["id"] == CODEX_THREAD_ID),
            })

        conn.close()
    except Exception:
        return []

    return rows

def session_day(s):
    ts = s.get("timestamp", "")
    return ts[:10] if ts else ""

def print_today():
    today = date.today().isoformat()

    claude = [s for s in load_claude_sessions() if session_day(s) == today]
    codex = [s for s in load_codex_sessions() if session_day(s) == today]

    rows = claude + codex
    rows.sort(key=lambda x: x.get("timestamp", ""))

    print(f"\n  AI token usage for {today}\n")

    if not rows:
        print("  No sessions recorded today.")
        return

    hdr = f"  {'TOOL':<7} {'SESSION-ID':<36} {'PROJECT':<18} {'MODEL':<16} {'INPUT':>10} {'OUTPUT':>10} {'CACHE_R':>10} {'CACHE_W':>10} {'MAIN_TOTAL':>12} {'FULL_TOTAL':>12} {'COST':>10}"
    sep = "  " + "-" * (len(hdr) - 2)
    print(hdr)
    print(sep)

    totals = defaultdict(int)
    claude_total_cost = 0.0

    for s in rows:
        tool = s["tool"]
        sid = s.get("session_id", "")[:36]
        project = short_project(s.get("cwd", ""))[:18]
        model = s.get("model", "sonnet-4.6" if tool == "claude" else "")[:16]
        live = "*" if s.get("live") else ""

        if tool == "claude":
            inp = s.get("input_tokens", 0)
            out = s.get("output_tokens", 0)
            cr = s.get("cache_read_input_tokens", 0)
            cw = s.get("cache_creation_input_tokens", 0)
            main_total = inp + out
            full_total = claude_full_tokens(inp, out, cr, cw)
            c = claude_cost(inp, out, cr, cw)
            cost_text = f"${c:>8.4f}"

            totals["claude_input"] += inp
            totals["claude_output"] += out
            totals["claude_cache_r"] += cr
            totals["claude_cache_w"] += cw
            totals["claude_main_total"] += main_total
            totals["claude_full_total"] += full_total
            claude_total_cost += c

            print(f"  {tool:<7} {sid:<36} {project:<18} {model:<16} {fmt(inp):>10} {fmt(out):>10} {fmt(cr):>10} {fmt(cw):>10} {fmt(main_total):>12} {fmt(full_total):>12} {cost_text:>10} {live}")

        else:
            total = s.get("total_tokens", 0)
            totals["codex_total"] += total

            print(f"  {tool:<7} {sid:<36} {project:<18} {model:<16} {'-':>10} {'-':>10} {'-':>10} {'-':>10} {fmt(total):>12} {fmt(total):>12} {'n/a':>10} {live}")

    print(sep)

    combined_main_total = totals["claude_main_total"] + totals["codex_total"]
    combined_full_total = totals["claude_full_total"] + totals["codex_total"]

    print(f"  {'TOTAL':<7} {'':<36} {'':<18} {'':<16} {fmt(totals['claude_input']):>10} {fmt(totals['claude_output']):>10} {fmt(totals['claude_cache_r']):>10} {fmt(totals['claude_cache_w']):>10} {fmt(combined_main_total):>12} {fmt(combined_full_total):>12} ${claude_total_cost:>8.4f}")
    print()
    print("  * live session")
    print("  MAIN_TOTAL = Claude input + output; Codex reported total from local DB.")
    print("  FULL_TOTAL = Claude input + output + cache read + cache write; Codex reported total from local DB.")
    print("  Codex cost is n/a because local Codex DB exposes total tokens only, not input/output/cache split.")
    print(f"  Claude pricing: Sonnet 4.6 — input ${CLAUDE_PRICE['input']}/M output ${CLAUDE_PRICE['output']}/M cache-r ${CLAUDE_PRICE['cache_read']}/M cache-w ${CLAUDE_PRICE['cache_write']}/M\n")

def print_daily():
    rows = load_claude_sessions() + load_codex_sessions()

    by_day = defaultdict(lambda: defaultdict(int))
    claude_cost_by_day = defaultdict(float)

    for s in rows:
        day = session_day(s)
        if not day:
            continue

        by_day[day]["sessions"] += 1

        if s["tool"] == "claude":
            by_day[day]["claude_sessions"] += 1
        elif s["tool"] == "codex":
            by_day[day]["codex_sessions"] += 1

        if s["tool"] == "claude":
            inp = s.get("input_tokens", 0)
            out = s.get("output_tokens", 0)
            cr = s.get("cache_read_input_tokens", 0)
            cw = s.get("cache_creation_input_tokens", 0)
            by_day[day]["claude_input"] += inp
            by_day[day]["claude_output"] += out
            by_day[day]["claude_cache_r"] += cr
            by_day[day]["claude_cache_w"] += cw
            by_day[day]["claude_main_total"] += inp + out
            by_day[day]["claude_full_total"] += claude_full_tokens(inp, out, cr, cw)
            claude_cost_by_day[day] += claude_cost(inp, out, cr, cw)
        else:
            by_day[day]["codex_total"] += s.get("total_tokens", 0)

    today = date.today().isoformat()

    print("\n  Daily AI token usage\n")

    hdr = f"  {'DATE':<12} {'CL_SESS':>7} {'CX_SESS':>7} {'CLAUDE_IN':>12} {'CLAUDE_OUT':>12} {'CACHE_R':>12} {'CACHE_W':>12} {'CLAUDE_FULL':>13} {'CODEX_TOTAL':>14} {'MAIN_TOTAL':>14} {'FULL_TOTAL':>14} {'CLAUDE_COST':>12}"
    sep = "  " + "-" * (len(hdr) - 2)
    print(hdr)
    print(sep)

    grand = defaultdict(int)
    grand_cost = 0.0

    for day in sorted(by_day):
        d = by_day[day]
        main_total = d["claude_main_total"] + d["codex_total"]
        full_total = d["claude_full_total"] + d["codex_total"]
        marker = " ◀ today" if day == today else ""

        print(
            f"  {day:<12} "
            f"{d['claude_sessions']:>7} "
            f"{d['codex_sessions']:>7} "
            f"{fmt(d['claude_input']):>12} "
            f"{fmt(d['claude_output']):>12} "
            f"{fmt(d['claude_cache_r']):>12} "
            f"{fmt(d['claude_cache_w']):>12} "
            f"{fmt(d['claude_full_total']):>13} "
            f"{fmt(d['codex_total']):>14} "
            f"{fmt(main_total):>14} "
            f"{fmt(full_total):>14} "
            f"${claude_cost_by_day[day]:>10.4f}"
            f"{marker}"
        )

        for k, v in d.items():
            grand[k] += v
        grand_cost += claude_cost_by_day[day]

    print(sep)
    print(
        f"  {'TOTAL':<12} "
        f"{grand['claude_sessions']:>7} "
        f"{grand['codex_sessions']:>7} "
        f"{fmt(grand['claude_input']):>12} "
        f"{fmt(grand['claude_output']):>12} "
        f"{fmt(grand['claude_cache_r']):>12} "
        f"{fmt(grand['claude_cache_w']):>12} "
        f"{fmt(grand['claude_full_total']):>13} "
        f"{fmt(grand['codex_total']):>14} "
        f"{fmt(grand['claude_main_total'] + grand['codex_total']):>14} "
        f"{fmt(grand['claude_full_total'] + grand['codex_total']):>14} "
        f"${grand_cost:>10.4f}"
    )

    print()
    print("  MAIN_TOTAL = Claude input + output; Codex reported total from local DB.")
    print("  FULL_TOTAL = Claude input + output + cache read + cache write; Codex reported total from local DB.")
    print("  Codex total comes from ~/.codex/state_5.sqlite threads.tokens_used.")
    print("  Codex input/output/cache split is not available in the current local DB schema.")
    print(f"  Claude pricing: Sonnet 4.6 — input ${CLAUDE_PRICE['input']}/M output ${CLAUDE_PRICE['output']}/M cache-r ${CLAUDE_PRICE['cache_read']}/M cache-w ${CLAUDE_PRICE['cache_write']}/M\n")

def print_current():
    printed = False

    if CODEX_THREAD_ID and CODEX_DB.exists():
        for s in load_codex_sessions():
            if s.get("session_id") == CODEX_THREAD_ID:
                print("\n  Current Codex session\n")
                print(f"  session_id:       {s['session_id']}")
                print(f"  project:          {s.get('cwd','')}")
                print(f"  model:            {s.get('model','')}")
                print(f"  reasoning_effort: {s.get('reasoning_effort','')}")
                print(f"  tokens_used:      {fmt(s.get('total_tokens',0))}")
                print(f"  updated_at:       {s.get('timestamp','')}")
                print()
                printed = True

    if not printed:
        print("No current Codex session detected.")
        print("Run this inside Codex for current-session Codex tracking.")

if MODE == "today":
    print_today()
elif MODE == "daily":
    print_daily()
elif MODE == "current":
    print_current()
PYEOF
