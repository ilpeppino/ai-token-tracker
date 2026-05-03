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
        set tabTitle to title of t

        tell t
          set pageText to execute javascript "document.body.innerText"
        end tell

        do shell script "echo " & quoted form of ("==============================" & linefeed & "TITLE: " & tabTitle & linefeed & "URL: " & tabUrl & linefeed & "==============================" & linefeed & pageText)
      end if
    end repeat
  end repeat

  if foundCount is 0 then
    do shell script "echo 'No matching ChatGPT Codex or Claude usage tabs found.'"
  end if
end tell
APPLESCRIPT
