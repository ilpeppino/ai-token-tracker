#!/usr/bin/env bash
set -euo pipefail

osascript <<'APPLESCRIPT'
tell application "Google Chrome"
  set foundCount to 0

  repeat with w in windows
    repeat with t in tabs of w
      set tabUrl to URL of t

      if tabUrl contains "chatgpt.com/codex/cloud/settings/analytics" or tabUrl contains "claude.ai/settings/usage" then
        set foundCount to foundCount + 1
        tell t to reload
      end if
    end repeat
  end repeat

  if foundCount is 0 then
    do shell script "echo 'No matching Codex/Claude usage tabs found to refresh.'"
  else
    do shell script "echo 'Refreshed ' & " & foundCount & " & ' usage tab(s).'"
  end if
end tell
APPLESCRIPT

# Give dynamic dashboards time to reload.
sleep 8
