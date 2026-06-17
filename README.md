# AI Token Tracker

Local dashboard for tracking AI coding-agent usage across Claude Code and Codex CLI.

The project normalizes local usage into a practical measurement unit called **Toktok**. Toktok does **not** claim to be an official vendor token. It is a local, empirical usage unit used to compare agentic AI consumption across tools and correlate that usage with vendor-reported quota percentages.

## Goals

- Track Claude Code and Codex CLI usage locally.
- Compare usage across tools using a unified Toktok view.
- Estimate Claude cost where token-class pricing is available.
- Track Codex usage where only local reported totals are available.
- Capture vendor usage percentages from authenticated browser pages.
- Build calibration data between Toktok usage and vendor quota percentages.
- Support future providers and models without redesigning the whole tracker.

## Location

Project folder:

```text
<repo clone path>
```

Compatibility symlink:

```text
~/.ai-token-tracker
```

## Project Layout

```text
ai-tokens          Main command dispatcher.
install.sh        Local installer for the command and virtual environment.
scripts/install-telegram-service.sh  Installs the Ubuntu user service for Telegram.
scripts/usage-page-scrape.py  Playwright scraper for authenticated browser usage pages.
run.sh            Compatibility wrapper for dashboard startup.
sync.sh           Compatibility wrapper for local usage sync.
export.sh         Compatibility wrapper for export.
scripts/          Python and shell implementation scripts.
exports/          Generated CSV/JSON exports.
usage-dumps/      Generated browser page text dumps.
usage.sqlite      Local SQLite database.
```

## Commands

Open dashboard:

```bash
ai-tokens dashboard
```

Sync local usage data:

```bash
ai-tokens sync
```

Refresh usage dashboard tabs and scrape latest browser quota data:

```bash
ai-tokens refresh
```

Check usage-change notifications manually:

```bash
ai-tokens notify
```

Poll usage-change notifications every 5 minutes:

```bash
ai-tokens poll-notify
```

Check reset notifications manually:

```bash
ai-tokens reset-notify
```

Run sync + alerts + reset checks:

```bash
ai-tokens sync-notify
```

Run Telegram interactive bot:

```bash
ai-tokens bot
```

Run the interactive bot with background usage polling every 5 minutes:

```bash
ai-tokens telegram
```

Run the Telegram service alias:

```bash
ai-tokens service
```

Export CSV/JSON backup:

```bash
ai-tokens export
```

Terminal stats:

```bash
ai-tokens stats today
ai-tokens stats daily
```

Direct local scripts:

```bash
./run.sh
./sync.sh
./export.sh
./scripts/sync-usage.py
./scripts/sync-usage-percentages.py
./scripts/usage-page-scrape.py all
./scripts/telegram.py notify
./scripts/telegram.py poll-notify
./scripts/telegram.py reset-notify
./scripts/telegram.py bot
./scripts/telegram.py telegram
./scripts/install-telegram-service.sh
```

## Data Sources

### Claude Code

Reads:

```text
~/.claude/token-usage.jsonl
~/.claude/projects/*/*.jsonl
```

Claude provides detailed local usage fields:

- Input tokens
- Output tokens
- Cache read tokens
- Cache write tokens
- Estimated cost, based on configured pricing

### Codex CLI

Reads:

```text
~/.codex/state_5.sqlite
```

Codex currently provides:

- Reported total tokens from local SQLite
- Model name
- Reasoning effort
- Project path
- Session/thread metadata

Codex does not currently expose locally:

- Input token split
- Output token split
- Cache read/write split
- Cost breakdown

### Browser Usage Pages

The tracker reads authenticated browser usage pages through a local Playwright scraper attached to an already running Chrome instance over DevTools.

Current target pages:

```text
https://chatgpt.com/codex/cloud/settings/analytics
https://claude.ai/settings/usage
```

These pages are used to capture vendor-reported quota percentages such as:

- 5-hour window usage
- Weekly usage
- Remaining quota / used quota
- Reset timestamps / countdowns when exposed by provider pages

The browser usage extraction is best-effort and depends on page text remaining parseable.

The browser must be started with remote debugging enabled, for example:

```bash
google-chrome --remote-debugging-port=9222
```

The scraper connects to that browser, reads the logged-in Codex and Claude tabs, and writes dumps without opening new pages. Set `AI_TOKENS_CHROME_CDP_URL` if you expose the DevTools endpoint on a different port or host.

## Local Database

Main SQLite database:

```text
~/.ai-token-tracker/usage.sqlite
```

Actual path, via symlink:

```text
<repo clone path>/usage.sqlite
```

Primary usage table:

```text
usage_sessions
```

Browser usage percentage snapshots:

```text
usage_percentage_snapshots
```

Calibration view:

```text
calibration_estimates
```

Quota forecast view:

```text
quota_forecast
```

## Dashboard Features

- Daily usage charts
- Tool split breakdown
- Project split breakdown
- 7-day moving averages
- Month-to-date usage
- Projected monthly usage
- Claude cost estimates
- Top sessions table
- Budget and forecast widgets
- Browser-derived quota percentage snapshots
- Calibration estimates between Toktok and vendor quota percentages
- Quota forecast / depletion prediction
- 5-hour and weekly reset countdowns
- Telegram alert integration
- Telegram interactive status bot
- Automatic reset notifications
- Automatic browser tab refresh before quota scraping

## Metric Definitions

### Toktok

Toktok is the normalized local usage unit used by this tracker.

It is intentionally not treated as a real vendor token. It is a comparable local unit that can be correlated with vendor-reported usage percentages.

### MAIN_TOTAL

Claude:

```text
input + output
```

Codex:

```text
reported total tokens from local Codex SQLite
```

### FULL_TOTAL

Claude:

```text
input + output + cache read + cache write
```

Codex:

```text
reported total tokens from local Codex SQLite
```

### Claude Cost

Claude cost is estimated from configured pricing values.

### Codex Cost

Codex cost is currently unavailable because the local Codex database does not expose input/output/cache categories or billing-equivalent usage.

## Calibration Concept

The tracker can correlate Toktok usage with vendor-reported quota percentage.

Example workflow:

1. Start from a fresh 5-hour or weekly quota window.
2. Run a coding-agent task.
3. Sync local Toktok usage.
4. Capture the vendor usage percentage from the browser usage page.
5. Store a calibration snapshot.
6. Estimate how many Toktok correspond to 1% of quota usage.
7. Estimate current 5-hour and weekly capacity.

Example estimate:

```text
52,000,000 Toktok used / 97% weekly usage = ~53,600,000 Toktok estimated weekly capacity
```

This is an empirical estimate, not an official vendor quota value.

## Browser Scraping Notes

The browser scraper does not log in automatically.

Recommended flow:

1. Open the ChatGPT Codex analytics page in normal Google Chrome.
2. Open the Claude usage page in normal Google Chrome.
3. Make sure both pages are already authenticated.
4. Enable Chrome menu option:

```text
View > Developer > Allow JavaScript from Apple Events
```

5. Run the local scraper scripts to dump and parse visible page text.

This avoids storing credentials and avoids automating login or CAPTCHA flows.

## Limitations

- Toktok is not an official vendor token.
- Vendor quota calculations are opaque and may change.
- Claude cost is estimated from configured pricing.
- Codex cost is unavailable from the current local Codex database.
- Codex totals may not equal official billing or quota accounting.
- Browser usage extraction can break if page text changes.
- 5-hour and weekly window alignment is currently estimated from local timestamps unless exact reset data is captured.
- Calibration quality depends on clean observations and repeated snapshots.
- Browser refresh/scraping requires the authenticated usage tabs to remain open in Google Chrome.

## Backup / Export

Exports are stored in:

```text
~/.ai-token-tracker/exports/
```

Export command:

```bash
ai-tokens export
```

## Background Automation

### Ubuntu Telegram Service

On Ubuntu, the Telegram bot runs as a `systemd --user` service:

```bash
./scripts/install-telegram-service.sh
```

Logs:

```bash
journalctl --user -u ai-token-tracker-telegram.service -f
```

The service runs the interactive bot, performs `ai-tokens sync` on its polling cycle, and sends notifications from the refreshed local database. The browser scraper reads the already-open Chrome tabs through DevTools, so Chrome must be started with remote debugging enabled before the service runs.
To keep the service alive after logout or reboot, enable lingering with `sudo loginctl enable-linger <user>`.

### macOS Sync / Forecast / Notifications

The project can also run fully automated on macOS using LaunchAgents.

Runs every 10 minutes:

```text
~/Library/LaunchAgents/com.peppe.ai-token-sync.plist
```

Per run it executes:

1. Refresh open Codex/Claude browser usage tabs
2. Dump browser quota page content
3. Sync local usage data
4. Sync browser quota snapshots
5. Rebuild calibration estimates
6. Rebuild quota forecast
7. Send Telegram usage-change and reset notifications

Logs:

```bash
tail -f /tmp/ai-token-sync.out /tmp/ai-token-sync.err
```

### Telegram Interactive Bot

Runs continuously in background:

```text
~/Library/LaunchAgents/com.peppe.ai-token-telegram-bot.plist
```

Logs:

```bash
tail -f /tmp/ai-token-telegram-bot.out /tmp/ai-token-telegram-bot.err
```

### Service Management

```bash
launchctl list | grep ai-token
```

## Reinstall

From the project folder:

```bash
./install.sh
```

## Roadmap

- Add richer calibration history charts.
- Add confidence scoring for calibration estimates.
- Add manual calibration entry command.
- Add provider/model configuration files.
- Add historical pricing support.
- Add weekly/monthly summary reports.
- Add Telegram remote threshold configuration commands.
- Add Telegram mute/snooze alert commands.
- Add richer reset prediction / drift correction.
- Add multi-user / multi-chat Telegram support.
