# AI Token Tracker

Local Mac dashboard for tracking Claude Code and Codex CLI token usage.

## Location

Project folder:

/Volumes/DevSSD/projects/ai-token-tracker

Compatibility symlink:

~/.ai-token-tracker

## Commands

Open dashboard:

ai-tokens dashboard

Sync usage data:

ai-tokens sync

Export CSV/JSON backup:

ai-tokens export

Terminal stats:

ai-tokens stats today
ai-tokens stats daily

Direct local scripts:

./run.sh
./sync.sh
./export.sh

## Data Sources

Claude reads:

~/.claude/token-usage.jsonl
~/.claude/projects/*/*.jsonl

Claude provides:

- Input tokens
- Output tokens
- Cache read tokens
- Cache write tokens
- Estimated cost

Codex reads:

~/.codex/state_5.sqlite

Codex provides:

- Reported total tokens only

Codex does not provide:

- Input/output/cache split
- Cost breakdown

## Local Database

~/.ai-token-tracker/usage.sqlite

Actual path, via symlink:

/Volumes/DevSSD/projects/ai-token-tracker/usage.sqlite

## Dashboard Features

- Daily usage charts
- Tool split breakdown
- Project split breakdown
- 7-day moving averages
- Month-to-date usage
- Projected monthly usage
- Claude cost estimates
- Top sessions table

## Metric Definitions

MAIN_TOTAL:

Claude: input + output

Codex: reported total tokens

FULL_TOTAL:

Claude: input + output + cache read + cache write

Codex: reported total tokens

## Limitations

- Claude cost is estimated from configured Sonnet pricing
- Codex cost is unavailable because local DB does not expose token category breakdown
- Codex total comes from local SQLite and may not equal an official billing/API usage number

## Backup / Export

Exports are stored in:

~/.ai-token-tracker/exports/

## Auto Sync

LaunchAgent runs sync automatically every 5 minutes:

~/Library/LaunchAgents/com.peppe.ai-token-sync.plist

## Troubleshooting

Check sync logs:

tail -f /tmp/ai-token-sync.out /tmp/ai-token-sync.err

Restart LaunchAgent:

launchctl unload ~/Library/LaunchAgents/com.peppe.ai-token-sync.plist
launchctl load ~/Library/LaunchAgents/com.peppe.ai-token-sync.plist

## Reinstall

From project folder:

./install.sh
