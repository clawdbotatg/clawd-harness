# Demo 1 — iPhone voice agent (OS echo cancellation)

> **Status: not started.** Record the speaker loop test results (README.md)
> here when done.

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
