# tools/kiosk — keep a hands-off wall display on its home screen

**Box:** clawd-sat (`ClawdSats-Mac-mini`, user `clawdsat`) is a wall/cabinet
display with **no keyboard or mouse**. Its home screen is **gizmo**
(`~/gizmo`, served on `http://127.0.0.1:7912`), shown by a Chrome `--app`
window on the `~/.gizmo-chrome` profile that `~/gizmo/kiosk-keeper.sh`
(launchd `com.gizmo.kiosk`) launches and relaunches when the page loses its
WebSocket screens.

**The hole (2026-09-02):** the keeper only counts connected screens, so
anything that opens *in front of* the page is invisible to it. A launchd job
(`com.leftclaw.auth-refresh` → `cont auth ensure` → `claude setup-token`)
opened its OAuth login in the kiosk profile — the only visible Chrome, which
is also the default https handler — and the login page sat on the glass for
hours, waiting for a human that can never come. Earlier session files showed
it had happened before.

**The guard:** `kiosk-guard.py`, launchd `com.gizmo.kioskguard`, every 2 s:

1. closes every page on the kiosk profile that isn't the home URL (Chrome
   DevTools Protocol on `--remote-debugging-port=9223`);
2. reopens the home page if it's gone, forces its window back to fullscreen,
   activates it;
3. if another *app* is frontmost, brings the kiosk Chrome to the front **by
   pid** (Carbon `SetFrontProcessWithOptions` via ctypes);
4. kills `claude setup-token` / `claude login` processes older than 3 min —
   they can't complete without input devices and they wedge their launchd job.

Why not AppleScript: `osascript` needs Accessibility/Automation consent, which
only a human clicking System Settings on that box can grant. Everything the
guard does works from a launchd agent with no TCC grant.

Install / update on the box (idempotent):

```bash
cd ~/clawd/clawd-harness && git pull --ff-only && bash tools/kiosk/install-sat.sh
```

The agent runs the script from the checkout, so a push here reaches the box
with the harness auto-pull (~5 min). Log: `~/gizmo/state/kioskguard.log`
(state changes only). One-shot check: `python3 tools/kiosk/kiosk-guard.py --once`.

**Still open on sat:** the auth-refresh job will keep firing `setup-token`
every 6 h (there is no `Claude Code-credentials` keychain entry for
`~/.claude` on that box); the guard closes the popup within seconds and kills
the hung process, but the leftclaw wrangler on sat has no token until someone
logs it in for real or points `cont` at one of the `~/.clawd-accounts` logins.
