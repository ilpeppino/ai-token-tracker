#!/usr/bin/env bash
set -euo pipefail

BASE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BASE"

OUT_DIR="$BASE/usage-dumps"
mkdir -p "$OUT_DIR"

osascript - "$OUT_DIR" <<'APPLESCRIPT'
on run argv
set outDir to item 1 of argv

tell application "Google Chrome"
  set foundCount to 0

  repeat with w in windows
    repeat with t in tabs of w
      set tabUrl to URL of t

      if tabUrl contains "chatgpt.com/codex/cloud/settings/analytics" or (tabUrl contains "claude.ai" and (tabUrl contains "settings/usage" or tabUrl contains "settings/plan")) then
        set foundCount to foundCount + 1

        set tabTitle to title of t

        tell t
          set pageText to execute javascript "document.body.innerText"
        end tell

        set safeName to do shell script "python3 - <<'PY'\nimport re\nprint(re.sub(r'[^a-zA-Z0-9]+','_', '''" & tabTitle & "''')[:80])\nPY"

        set dumpPath to outDir & "/" & safeName & ".txt"
        do shell script "cat > " & quoted form of dumpPath & " <<'EOF'\n" & pageText & "\nEOF"

        log "Dumped: " & safeName & ".txt"
      end if
    end repeat
  end repeat

  if foundCount is 0 then
    log "No matching tabs found."
  end if
end tell
end run
APPLESCRIPT
