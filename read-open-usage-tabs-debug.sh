#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="./usage-dumps"
mkdir -p "$OUT_DIR"

osascript <<'APPLESCRIPT'
tell application "Google Chrome"
  set foundCount to 0

  repeat with w in windows
    repeat with t in tabs of w
      set tabUrl to URL of t

      if tabUrl contains "chatgpt.com/codex/cloud/settings/analytics" or tabUrl contains "claude.ai/settings/usage" then
        set foundCount to foundCount + 1

        set tabTitle to title of t

        tell t
          set pageText to execute javascript "document.body.innerText"
        end tell

        set safeName to do shell script "python3 - <<'PY'\nimport re\nprint(re.sub(r'[^a-zA-Z0-9]+','_', '''" & tabTitle & "''')[:80])\nPY"

        do shell script "cat > ./usage-dumps/" & safeName & ".txt <<'EOF'\n" & pageText & "\nEOF"

        log "Dumped: " & safeName & ".txt"
      end if
    end repeat
  end repeat

  if foundCount is 0 then
    log "No matching tabs found."
  end if
end tell
APPLESCRIPT
