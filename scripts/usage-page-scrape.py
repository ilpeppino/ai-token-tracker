#!/usr/bin/env python3
"""Scrape authenticated browser usage pages from an already running Chrome.

This attaches to a Chrome/Chromium instance through the DevTools protocol and
reads the existing logged-in tabs. It does not open new pages.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

PROJECT_DIR = Path(__file__).resolve().parents[1]
DUMP_DIR = PROJECT_DIR / "usage-dumps"
CHROME_CDP_URL = os.environ.get("AI_TOKENS_CHROME_CDP_URL", "http://127.0.0.1:9222")

TARGETS = {
    "codex": "chatgpt.com/codex/cloud/settings/analytics",
    "claude": "claude.ai/settings/usage",
}

PAGE_MARKERS = {
    "codex": "Codex Analytics",
    "claude": "Plan Usage Limits",
}


def dump_path_for(name: str) -> Path:
    return DUMP_DIR / f"{name.capitalize()}.txt"


def write_dump(name: str, url: str, text: str) -> Path:
    DUMP_DIR.mkdir(parents=True, exist_ok=True)
    observed_at = datetime.now(timezone.utc).isoformat()
    output = dump_path_for(name)
    output.write_text(
        "\n".join(
            [
                f"# provider={name}",
                f"# marker={PAGE_MARKERS[name]}",
                f"# url={url}",
                f"# observed_at={observed_at}",
                "",
                text.strip(),
                "",
            ]
        )
    )
    return output


def normalize_url(url: str) -> str:
    return url.lower().strip()


def page_matches(name: str, url: str) -> bool:
    return TARGETS[name] in normalize_url(url)


def iter_existing_pages(browser) -> Iterable[tuple[str, Any]]:
    for context in browser.contexts:
        for page in context.pages:
            url = getattr(page, "url", "")
            if not url:
                continue
            for name in TARGETS:
                if page_matches(name, url):
                    yield name, page


def scrape_page(name: str, page) -> Path:
    url = page.url
    print(f"Scraping {name}: {url}", flush=True)

    try:
        text = page.locator("body").inner_text(timeout=30000)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(f"{name}: failed to read page text: {exc}") from exc

    lowered = text.lower()
    if "log in" in lowered or "sign in" in lowered or "continue with google" in lowered:
        print(f"{name}: login page detected in existing tab; dump still written for inspection.", flush=True)

    dump_file = write_dump(name, url, text)
    print(f"{name}: wrote {dump_file}", flush=True)
    return dump_file


def main() -> None:
    provider = sys.argv[1].lower() if len(sys.argv) > 1 else "all"

    if provider not in {"all", "codex", "claude"}:
        raise SystemExit("Usage: scripts/usage-page-scrape.py [all|codex|claude]")

    wanted = {provider} if provider in TARGETS else set(TARGETS)

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CHROME_CDP_URL)
        except Exception as exc:
            raise SystemExit(
                "Could not connect to Chrome via DevTools at "
                f"{CHROME_CDP_URL}. Start Chrome with "
                "'--remote-debugging-port=9222 --user-data-dir=<your profile>' "
                "so this script can read the existing logged-in tabs."
            ) from exc

        try:
            seen: set[int] = set()
            found = 0

            for name, page in iter_existing_pages(browser):
                if name not in wanted:
                    continue

                page_id = id(page)
                if page_id in seen:
                    continue

                seen.add(page_id)
                scrape_page(name, page)
                found += 1

            if found == 0:
                raise SystemExit(
                    "No matching existing Chrome tabs were found. "
                    f"Looked for {', '.join(sorted(wanted))} in tabs from {CHROME_CDP_URL}.\n"
                    "Open the target pages in the already running Chrome profile, then rerun."
                )
        finally:
            pass

    print("Done.")


if __name__ == "__main__":
    main()
