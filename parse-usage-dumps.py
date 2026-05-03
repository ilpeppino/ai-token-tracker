#!/usr/bin/env python3
import re
from pathlib import Path

DUMP_DIR = Path("./usage-dumps")


def pct_after(label, text, mode="used"):
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
    }


def parse_claude(text):
    five_used = pct_after("Current session", text, "used")
    weekly_used = pct_after("Weekly limits", text, "used")

    return {
        "provider": "claude",
        "five_hour_used_pct": five_used,
        "weekly_used_pct": weekly_used,
    }


def main():
    for file in DUMP_DIR.glob("*.txt"):
        text = file.read_text()

        low = text.lower()

        if "codex analytics" in low:
            result = parse_codex(text)
        elif "plan usage limits" in low:
            result = parse_claude(text)
        else:
            continue

        print()
        print("=" * 40)
        print(result["provider"].upper())
        print("=" * 40)
        print(f"5h Used:     {result['five_hour_used_pct']}%")
        print(f"Weekly Used: {result['weekly_used_pct']}%")
        print()

if __name__ == "__main__":
    main()
