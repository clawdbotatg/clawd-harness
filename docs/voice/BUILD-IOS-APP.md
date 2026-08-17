# Demo 1 — iPhone voice agent (OS echo cancellation)

> **Status: STOPPED BY USER mid-debug (2026-08-16), app not yet working.**
> Full honest record: `POSTMORTEM.md` in the app repo. Shipped broken to the
> user four times (stale IP default, missing `NSLocalNetworkUsageDescription`,
> stored-empty URL shadowing the default, and the open bug). The earlier
> "shape A echo FAIL" entry here was wrong — that echo loop was phone
> **Safari**, not the app. Verified: full WS protocol replay from the Mac
> passes (real API answered); remote `/log` diagnostics + no-Xcode-account
> sign/install/launch pipeline work. OPEN: native-mode mic tap delivers zero
> callbacks / engine setup dies uncaught after the `route:` log on the
> iPhone 17 Pro — next step is one console-attached `devicectl` launch to
> read the exception (command in POSTMORTEM.md). Repo:
> **[clawdbotatg/clawd-iphone-gpt-chat](https://github.com/clawdbotatg/clawd-iphone-gpt-chat)**
> — BOTH shapes ship in one app behind a segmented control (A · WKWebView wrap
> primary, B · native AVAudioEngine+WebSocket fallback), so one dev-build
> session tests both if A echoes. `.voiceChat` set at launch and re-asserted on
> route changes (WebKit reconfigures the session when getUserMedia starts).
>
> **Verified from the Mac:** token server (vendored gpt-voice `serve.py`,
> ports 8124/8444 to coexist with the reference on 8123/8443,
> `server/run-server.sh` pulls the key from the credential store at runtime)
> mints live; headless-Chromium E2E of the wrapped page reached
> `🟢 live` against real OpenAI (synthesized audio track — this Mac has no TCC
> mic grant for any Chrome, so getUserMedia hangs even on fake devices).
> Swift is `swiftc -parse` clean only — **no Xcode on this Mac** (CLT only;
> installing needs sudo or an Apple ID, neither available to the agent), so it
> has never been compiled against the iOS SDK.
>
> **Speaker loop test: NOT RUN — needs a human + phone.** Steps 1–4 all
> pending. Austin is taking it to a machine that has Xcode — full runbook
> (server setup with a plain `.env`, build, test, where to record) is
> **`HANDOFF.md` in the app repo**. Record per-step PASS/FAIL and which
> shape (A or B) here.

## Goal

The smallest possible iPhone app you can talk to: OpenAI `gpt-realtime`
(semantic VAD, barge-in), with iOS's OS/hardware echo cancellation via
`AVAudioSession` mode **`.voiceChat`** — the one setting browsers can't reach
and the reason the ChatGPT app is full-duplex on a phone speaker. Ten minutes
of natural conversation on the iPhone's built-in speaker is the whole product.

Read `docs/voice/README.md` in clawd-harness first (context + the acceptance
test), and crib API mechanics from **github.com/clawdbotatg/gpt-voice** (the
verified-working reference; its `INTEGRATION.md` lists the traps).

## Build shape

**New repo** (suggest `clawdbotatg/gpt-voice-ios`). Two viable shapes — do A,
fall back to B only if A's bet fails:

**A. WKWebView wrap (fastest — reuses the verified web code).** A SwiftUI app
that is: `AVAudioSession` set to `.playAndRecord` / `.voiceChat` /
`.defaultToSpeaker` at launch, plus a WKWebView pointed at the gpt-voice demo
page served from the Mac. The bet being tested: the OS AEC applies to audio
flowing through the webview's WebRTC. Config that must be right:
- `NSMicrophoneUsageDescription` in Info.plist
- `allowsInlineMediaPlayback = true`, `mediaTypesRequiringUserActionForPlayback = []`
- getUserMedia needs a secure origin: use gpt-voice `serve.py`'s
  `ensure_cert()` self-signed-HTTPS-on-LAN (accept the cert warning once in
  Safari first), or bundle the HTML locally and only mint tokens remotely.

**B. Fully native (if A still echoes).** No webview: `AVAudioEngine` with
`setVoiceProcessingEnabled(true)` on the input node, stream PCM16 24kHz mono
to the OpenAI Realtime API over **WebSocket** (`input_audio_buffer.append` /
`response.output_audio.delta` — same events, base64 audio; no libwebrtc
needed), play deltas through the same engine. On
`input_audio_buffer.speech_started` while playing: flush local playback and
send `response.cancel` — that's barge-in. Note: the `output_audio_buffer.*`
events are WebRTC-only; over WS you know playback state because you're the
one playing.

Either way: UI is a start/stop button and a state word (LISTENING / SPEAKING).
Transcripts on screen if cheap. Nothing else.

## Key handling

`OPENAI_API_KEY` stays on the Mac in the token server (`serve.py` minting
ephemeral secrets); the app fetches `/token` over the LAN. Never embed the
real key in the app, even for a demo — ephemeral minting is already built in
the reference.

## Definition of done

1. Runs on a real iPhone (dev build / TestFlight, no App Store).
2. **The README speaker loop test passes on the iPhone's built-in speaker** —
   all four steps, especially: it never reacts to its own voice, and voice
   barge-in works.
3. Status block updated with per-step PASS/FAIL and which shape (A or B) it
   took — that answer feeds the real product decision later.
