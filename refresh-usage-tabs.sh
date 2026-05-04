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

  return "Browser reload requested for " & foundCount & " usage tab(s)."
end tell
APPLESCRIPT

# Give the browser-level reload time to complete before trying app-level refresh.
sleep 8

osascript <<'APPLESCRIPT'
tell application "Google Chrome"
  set clickedCount to 0
  set clickDetails to ""

  repeat with w in windows
    repeat with t in tabs of w
      set tabUrl to URL of t

      if tabUrl contains "claude.ai/settings/usage" then
        tell t
          set clickResult to execute javascript "(() => { var isVisible = function(el) { if (!el) return false; var rect = el.getBoundingClientRect(); var style = window.getComputedStyle(el); return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none'; }; var describe = function(el) { if (!el) return 'none'; var text = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 80); var aria = (el.getAttribute('aria-label') || '').trim(); var title = (el.getAttribute('title') || '').trim(); return el.tagName.toLowerCase() + ' text=' + text + ' aria=' + aria + ' title=' + title; }; var candidates = Array.from(document.querySelectorAll('button,[role=button]')).filter(isVisible); var target = candidates.find(function(el) { var text = (el.innerText || el.textContent || '').toLowerCase(); var label = ((el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')).toLowerCase(); var combined = text + ' ' + label; return combined.includes('refresh') || combined.includes('reload') || combined.includes('sync'); }); if (!target) { var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); var textNode = null; while (walker.nextNode()) { var value = walker.currentNode.nodeValue || ''; if (value.includes('Last updated')) { textNode = walker.currentNode; break; } } var container = textNode ? textNode.parentElement : null; for (var depth = 0; container && depth < 8 && !target; depth += 1) { var localButtons = Array.from(container.querySelectorAll('button,[role=button]')).filter(isVisible).filter(function(el) { return !el.disabled && el.getAttribute('aria-disabled') !== 'true'; }).sort(function(a, b) { var ar = a.getBoundingClientRect(); var br = b.getBoundingClientRect(); return (ar.width * ar.height) - (br.width * br.height); }); if (localButtons.length > 0) { target = localButtons[0]; break; } container = container.parentElement; } } if (target) { target.scrollIntoView({ block: 'center', inline: 'center' }); target.click(); return 'clicked:' + describe(target); } return 'not_found:visible_buttons=' + candidates.length; })()"
        end tell

        set clickDetails to clickDetails & clickResult & linefeed

        if clickResult starts with "clicked:" then
          set clickedCount to clickedCount + 1
        end if
      end if
    end repeat
  end repeat

  return "Claude internal refresh clicked on " & clickedCount & " tab(s)." & linefeed & clickDetails
end tell
APPLESCRIPT

# Give Claude's internal usage refresh time to fetch and repaint.
sleep 35
