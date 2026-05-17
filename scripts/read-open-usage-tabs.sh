#!/usr/bin/env bash
set -euo pipefail

BASE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BASE"

DUMP_DIR="$BASE/usage-dumps"

mkdir -p "$DUMP_DIR"

osascript <<'APPLESCRIPT' > "$DUMP_DIR/Claude.txt"
tell application "Google Chrome"
  repeat with w from 1 to count of windows
    set winRef to window w

    repeat with i from 1 to count of tabs of winRef
      set tabRef to tab i of winRef
      set tabUrl to URL of tabRef

      if tabUrl contains "claude.ai/settings/usage" then
        tell tabRef
          set resultText to execute javascript "(() => { var text = document.body.innerText; var lines = text.split('\\n').map(function(x) { return x.trim(); }).filter(Boolean); var interesting = lines.filter(function(line, idx) { var joined = lines.slice(Math.max(0, idx - 2), idx + 3).join(' | '); return joined.includes('Plan usage limits') || joined.includes('Current session') || joined.includes('Weekly limits') || joined.includes('All models') || joined.includes('Claude Design') || joined.includes('Last updated') || joined.includes('Additional features') || joined.includes('Daily included routine runs') || joined.includes('Extra usage') || line.includes('% used') || line.includes('Resets'); }); return interesting.join('\\n'); })()"
          return resultText
        end tell
      end if
    end repeat
  end repeat
end tell
APPLESCRIPT

osascript <<'APPLESCRIPT' > "$DUMP_DIR/Codex.txt"
tell application "Google Chrome"
  repeat with w from 1 to count of windows
    set winRef to window w

    repeat with i from 1 to count of tabs of winRef
      set tabRef to tab i of winRef
      set tabUrl to URL of tabRef

      if tabUrl contains "chatgpt.com/codex/cloud/settings/analytics" then
        tell tabRef
          set resultText to execute javascript "document.body.innerText"
          return resultText
        end tell
      end if
    end repeat
  end repeat
end tell
APPLESCRIPT

echo "Dumped usage pages to $DUMP_DIR"
