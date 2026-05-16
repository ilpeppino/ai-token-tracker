#!/usr/bin/env python3
import re
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = Path.home() / ".ai-token-tracker"
PROFILE_DIR = str(BASE / "browser-profile")

PAGES = {
    "codex": "https://chatgpt.com/codex/cloud/settings/analytics",
    "claude": "https://claude.ai/settings/usage",
}

def extract_percentages(text: str):
    matches = re.findall(r"(\d{1,3}(?:\.\d+)?)\s*%", text)
    values = []
    for m in matches:
        try:
            v = float(m)
            if 0 <= v <= 100:
                values.append(v)
        except ValueError:
            pass
    return values

def guess_usage_labels(text: str):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    interesting = []

    for i, line in enumerate(lines):
        low = line.lower()
        if "%" in line or "5-hour" in low or "5 hour" in low or "weekly" in low or "week" in low or "usage" in low or "limit" in low:
            context = lines[max(0, i - 2): min(len(lines), i + 3)]
            interesting.append(" | ".join(context))

    return interesting[:30]

def scrape(page, name, url):
    print(f"\n=== {name.upper()} ===")
    print(f"URL: {url}")

    page.goto(url, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(3000)

    text = page.locator("body").inner_text(timeout=30000)

    if "log in" in text.lower() or "sign in" in text.lower():
        print("Looks like you may need to log in in the opened browser window.")
        print("After logging in, rerun this script.")
        return

    percentages = extract_percentages(text)

    print("\nPercentages found:")
    if percentages:
        for p in percentages:
            print(f"  {p:g}%")
    else:
        print("  none found")

    print("\nRelevant text snippets:")
    snippets = guess_usage_labels(text)
    if snippets:
        for s in snippets:
            print(f"  - {s[:500]}")
    else:
        print("  no obvious usage snippets found")

def main():
    provider = sys.argv[1].lower() if len(sys.argv) > 1 else "all"

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            viewport={"width": 1440, "height": 1000},
        )

        page = browser.new_page()

        if provider == "all":
            targets = PAGES.items()
        elif provider in PAGES:
            targets = [(provider, PAGES[provider])]
        else:
            print("Usage: scripts/usage-page-scrape.py [all|codex|claude]")
            browser.close()
            sys.exit(1)

        for name, url in targets:
            try:
                scrape(page, name, url)
            except Exception as e:
                print(f"\n{name}: failed: {e}")

        print("\nDone.")
        print("If login was required, complete login in the opened browser, then rerun:")
        print("  python scripts/usage-page-scrape.py all")

        browser.close()

if __name__ == "__main__":
    main()
