# Build 1 — native iPhone shell: the existing web UI + OS echo cancellation

> **Status: not started.** Owner: (assign an agent). Record the speaker loop
> test result here when done.

## The bet

The entire voice stack already works (`https://h.atg.link` → PM tab → 🎙).
The ONLY thing the phone browser can't give it is OS-level acoustic echo
cancellation. iOS exposes that through one setting — `AVAudioSession` mode
**`.voiceChat`** — which applies hardware/OS AEC to the whole app's audio I/O,
including audio inside a WKWebView. So: wrap the existing page in a minimal
native shell, set that one setting, change nothing else. If the bet holds, the
phone speaker becomes full-duplex like the ChatGPT app.

Read `docs/voice/README.md` first (problem statement + the acceptance test).

## Shape of the build

- **New repo** (`clawdbotatg/clawd-voice-ios` or similar) — do NOT put an Xcode
  project inside clawd-harness.
- **Capacitor** (recommended over bare Swift: you get the WKWebView config,
  permissions plumbing, and a plugin system for the audio-session call). No
  React/build pipeline needed — there is no local web code; point Capacitor's
  `server.url` at `https://h.atg.link` so the app always runs the live fleet UI
  and inherits every future `git push` deploy with zero app updates.
- **One tiny native plugin** (or an existing community plugin if one fits):
  before/when a voice session starts, run roughly:

  ```swift
  let s = AVAudioSession.sharedInstance()
  try s.setCategory(.playAndRecord, mode: .voiceChat,
                    options: [.defaultToSpeaker, .allowBluetooth])
  try s.setActive(true)
  ```

  `.voiceChat` is the whole point of this build — it turns on the OS AEC.
  Setting it once at app launch is fine for v1 (it just pins the audio route);
  a fancier version listens for a JS event from the page.

## Config that must be right (each is a known WKWebView trap)

- `NSMicrophoneUsageDescription` in Info.plist (mic prompt text).
- `allowsInlineMediaPlayback = true`, `mediaTypesRequiringUserActionForPlayback
  = []` — the page autoplays the assistant's audio from a JS callback; without
  these it plays nothing.
- getUserMedia in WKWebView needs iOS 14.3+ and a secure origin —
  `https://h.atg.link` qualifies.
- The page detects touch (`pointer: coarse`) and defaults the 🎧 toggle OFF
  (half-duplex). For testing the bet, turn 🎧 ON in the HUD — that is the
  "trust the AEC" mode this build exists to make safe. If the build works,
  a follow-up can make the page detect the native shell (see hook below) and
  default 🎧 ON inside it.

## Risk #1 — auth: the fleet origin is passkey-gated

`h.atg.link` unlocks with a WebAuthn passkey, and `/pm/*` (the voice token
mint) rides that session (`pmt` cookie — see `fleet/relay.py::_pm_session_ok`).
**WKWebView's WebAuthn support is historically limited/absent** — verify early,
on a real device, before building anything else:

1. Load `https://h.atg.link` in a bare WKWebView. If the passkey ceremony
   completes → risk gone, proceed.
2. If not, pick a fallback (in order of preference):
   a. Capacitor/native WebAuthn bridge plugin (`ASAuthorizationPlatformPublicKey…`)
      that fulfills the page's `navigator.credentials.get`.
   b. A long-lived app session: do the passkey ceremony once in Safari, then
      hand the session token to the app (the relay's `session_valid` tokens —
      check their TTL in `fleet/relay.py`; extending TTL for an app-scoped
      token is a small relay change, coordinate before making it).
   Do NOT weaken the passkey gate itself.

## Hook for the page (optional, small, lives in clawd-harness)

If the page needs to know it's inside the shell (to default 🎧 ON, or to hide
the "add to home screen" style chrome), the shell can inject
`window.__NATIVE_VOICE__ = true` via `WKUserScript` — mirror of the existing
`window.__FLEET__` pattern. Keep any index.html change tiny and behind that
flag, and run `tools/voiceprobe.mjs` + `python3 tools/shipcheck.py` if you
touch clawd-harness at all.

## Definition of done

1. App installs on Austin's iPhone (TestFlight or dev build — no App Store).
2. Passkey unlock works inside the app (or the chosen fallback does).
3. **The speaker loop test in `README.md` passes with 🎧 ON, on the phone
   speaker** — that's the whole bet. Record PASS/FAIL per step in Status.
4. Ordinary harness driving (tabs, terminal, PM chat) still works in the shell
   — it's the same page, so this is a smoke check, not a test matrix.

## What NOT to do

- No local copy/fork of index.html inside the app — `server.url` only. The
  fleet UI deploys by `git push`; a vendored copy rots in a week.
- No OpenAI key in the app. The mint endpoint exists precisely so clients
  never hold the real key.
- Don't fight WKWebView into being a general browser (downloads, popups, etc.)
  — this is a kiosk for one origin.
