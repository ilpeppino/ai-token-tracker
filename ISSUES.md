• The reason is simple: the Linux scraper cannot see your already-open Chrome tabs unless Chrome is exposing a DevTools endpoint. I checked http://127.0.0.1:9222 on this machine and it is not open, so the scraper has nothing to attach to.
  That is why the pipeline ends up with n/a and no forecast data.

  To make this work with the tabs you already use, you need to restart Chrome once with remote debugging enabled:

  pkill chrome || pkill google-chrome || true
  google-chrome --remote-debugging-port=9222

  Then, in that Chrome instance:

  1. Open the Codex usage tab.
  2. Open the Claude usage tab.
  3. Make sure you are logged in on both.
  4. Verify the endpoint is live:

  curl http://127.0.0.1:9222/json/list

  You should see the open tabs in the output. After that, restart the service:

  systemctl --user restart ai-token-tracker-telegram.service
  journalctl --user -u ai-token-tracker-telegram.service -f
