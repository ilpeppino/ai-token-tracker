#!/usr/bin/env bash
set -euo pipefail

BASE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BASE"

DUMP_DIR="$BASE/usage-dumps"

mkdir -p "$DUMP_DIR"

osascript - "$DUMP_DIR" <<'APPLESCRIPT'
on run argv
set dumpDir to item 1 of argv
set dumpPath to dumpDir & "/Claude.txt"

tell application "Google Chrome"
  set foundCount to 0

  repeat with w from 1 to count of windows
    set winRef to window w

    repeat with i from 1 to count of tabs of winRef
      set tabRef to tab i of winRef
      set tabUrl to URL of tabRef

      if tabUrl contains "claude.ai" and (tabUrl contains "settings/usage" or tabUrl contains "settings/plan") then
        set foundCount to foundCount + 1
        tell tabRef
          set resultText to execute javascript "(() => { var text = document.body.innerText || ''; var lines = text.split('\\n').map(function(x) { return x.trim(); }).filter(Boolean); var interesting = lines.filter(function(line, idx) { var joined = lines.slice(Math.max(0, idx - 4), idx + 5).join(' | '); return /plan|usage|limit|session|weekly|model|updated|feature|routine|extra|reset|remaining|used|included/i.test(joined) || /\\d{1,3}(?:\\.\\d+)?%/.test(line); }); return interesting.length ? interesting.join('\\n') : text; })()"
        end tell

        do shell script "cat > " & quoted form of dumpPath & " <<'EOF'\n" & resultText & "\nEOF"
        return "Dumped Claude usage page to " & dumpPath
      end if
    end repeat
  end repeat

  do shell script "cat > " & quoted form of dumpPath & " <<'EOF'\n# Claude usage tab not found. Open https://claude.ai/settings/usage in Google Chrome.\nEOF"
  return "Claude usage tab not found."
end tell
end run
APPLESCRIPT

osascript - "$DUMP_DIR" <<'APPLESCRIPT'
on run argv
set dumpDir to item 1 of argv
set dumpPath to dumpDir & "/Codex.txt"

tell application "Google Chrome"
  set foundCount to 0

  repeat with w from 1 to count of windows
    set winRef to window w

    repeat with i from 1 to count of tabs of winRef
      set tabRef to tab i of winRef
      set tabUrl to URL of tabRef

      if tabUrl contains "chatgpt.com/codex/cloud/settings/analytics" then
        set foundCount to foundCount + 1
        tell tabRef
          set resultText to execute javascript "document.body.innerText"
        end tell

        do shell script "cat > " & quoted form of dumpPath & " <<'EOF'\n" & resultText & "\nEOF"
        return "Dumped Codex usage page to " & dumpPath
      end if
    end repeat
  end repeat

  do shell script "cat > " & quoted form of dumpPath & " <<'EOF'\n# Codex usage tab not found. Open https://chatgpt.com/codex/cloud/settings/analytics in Google Chrome.\nEOF"
  return "Codex usage tab not found."
end tell
end run
APPLESCRIPT

echo "Dumped usage pages to $DUMP_DIR"
