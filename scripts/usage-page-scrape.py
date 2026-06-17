#!/usr/bin/env python3
"""Scrape authenticated browser usage pages into local dump files.

This is the Linux-capable replacement for the old AppleScript-based browser
pipeline. It uses Playwright to open the authenticated pages, reads the visible
body text, and writes the result to usage-dumps/*.txt for the existing parser.
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

PROJECT_DIR = Path(__file__).resolve().parents[1]
DUMP_DIR = PROJECT_DIR / "usage-dumps"
PROFILE_DIR = Path(os.environ.get("AI_TOKENS_BROWSER_PROFILE_DIR", Path.home() / ".ai-token-tracker" / "browser-profile"))
HEADLESS_ENV = os.environ.get("AI_TOKENS_BROWSER_HEADLESS")

PAGES = {
    "codex": "https://chatgpt.com/codex/cloud/settings/analytics",
    "claude": "https://claude.ai/settings/usage",
}

PAGE_MARKERS = {
    "codex": "Codex Analytics",
    "claude": "Plan Usage Limits",
}


def dump_path_for(name: str) -> Path:
    return DUMP_DIR / f"{name.capitalize()}.txt"


def resolve_headless() -> bool:
    if HEADLESS_ENV is not None:
        return HEADLESS_ENV.strip().lower() not in {"0", "false", "no", "off"}
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def detect_executable_path() -> str | None:
    for candidate in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
    ):
        path = shutil.which(candidate)
        if path:
            return path
    return None


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


def scrape_page(page, name: str, url: str) -> Path:
    print(f"Scraping {name}: {url}", flush=True)
    page.goto(url, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(3000)

    try:
        text = page.locator("body").inner_text(timeout=30000)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(f"{name}: failed to read page text: {exc}") from exc

    lowered = text.lower()
    if "log in" in lowered or "sign in" in lowered or "continue with google" in lowered:
        print(f"{name}: login page detected; dump still written for inspection.", flush=True)

    dump_file = write_dump(name, url, text)
    print(f"{name}: wrote {dump_file}", flush=True)
    return dump_file


def main() -> None:
    provider = sys.argv[1].lower() if len(sys.argv) > 1 else "all"

    if provider not in {"all", "codex", "claude"}:
        raise SystemExit("Usage: scripts/usage-page-scrape.py [all|codex|claude]")

    targets = PAGES.items() if provider == "all" else [(provider, PAGES[provider])]

    executable_path = detect_executable_path()
    if executable_path is None:
        print(
            "No Chrome/Chromium binary found on PATH. Install Google Chrome or Chromium, "
            "or provide a Playwright browser with `python -m playwright install chromium`.",
            file=sys.stderr,
        )

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        launch_kwargs = {
            "headless": resolve_headless(),
            "viewport": {"width": 1440, "height": 1000},
        }
        if executable_path is not None:
            launch_kwargs["executable_path"] = executable_path

        browser = p.chromium.launch_persistent_context(str(PROFILE_DIR), **launch_kwargs)
        try:
            page = browser.new_page()
            for name, url in targets:
                try:
                    scrape_page(page, name, url)
                except Exception as exc:
                    print(f"{name}: failed: {exc}", file=sys.stderr)
        finally:
            browser.close()

    print("Done.")


if __name__ == "__main__":
    main()
