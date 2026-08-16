# Demo 2 — Mac voice agent (FaceTime's echo cancellation)

> **Status: not started.** Record the speaker loop test results (README.md)
> here when done — including the Chrome baseline.

## Goal

The smallest possible native Mac app you can talk to: OpenAI `gpt-realtime`
(semantic VAD, barge-in), with macOS's OS-level echo cancellation — the
CoreAudio voice-processing unit FaceTime uses. Ten minutes of natural
conversation on the Mac's open speakers is the whole product.

Read `docs/voice/README.md` in clawd-harness first (context + the acceptance
test), and crib API mechanics from **github.com/clawdbotatg/gpt-voice** (the
verified-working reference; its `INTEGRATION.md` lists the traps).

## Do this first: the Chrome baseline

Run the reference gpt-voice web demo in desktop Chrome on the same Mac,
speakers on, and run the README speaker loop test. Desktop Chrome's AEC is the
same engine Google Meet trusts, and it may already pass. Record that result in
Status FIRST — the native build's value is measured against it. (Even if
Chrome passes, finish the native demo: it's also the seed of an always-on
menu-bar agent, and its AEC quality under louder volume / worse rooms is the
interesting comparison.)

## Build shape

**New repo** (suggest `clawdbotatg/gpt-voice-mac`). Swift. **No Electron, no
webview** — Electron is Chromium (same AEC as the browser, tells us nothing),
and the point of this demo is the native audio path:

- **Audio**: `AVAudioEngine`; `inputNode.setVoiceProcessingEnabled(true)`
  (this is the whole trick — it enables AEC/noise suppression/AGC via
  `kAudioUnitSubType_VoiceProcessingIO`), output through the same engine.
  Capture PCM16 mono 24kHz for OpenAI; play the response deltas.
- **Transport**: OpenAI Realtime over **WebSocket**, not WebRTC — same events,
  base64 PCM (`input_audio_buffer.append` in, `response.output_audio.delta`
  out). Vendoring libwebrtc buys nothing when the OS is doing the AEC.
  Caveat: `output_audio_buffer.*` events are WebRTC-only; over WS you track
  playback state yourself (you're the one rendering audio).
- **Barge-in**: on `input_audio_buffer.speech_started` while audio is playing,
  flush the local playback queue and send `response.cancel`. With the OS AEC
  running, `speech_started` should only ever be the human — that assumption
  IS the test.
- **Session config**: mirror gpt-voice's `serve.py` (`gpt-realtime`, voice
  `marin`, `semantic_vad` + `eagerness: auto`, transcription on).
- **UI**: menu-bar item (LSUIElement) or one tiny window — start/stop and a
  state word. Nothing else.

## Key handling

`OPENAI_API_KEY` from the environment (or mint ephemeral secrets via the
gpt-voice `serve.py` running locally). Fine for a local demo binary; don't
commit the key anywhere.

## Definition of done

1. Builds and runs on the Mac (Xcode or `swift build`; no signing ceremony
   needed for a local app).
2. **The README speaker loop test passes on open Mac speakers** — all four
   steps, especially voice barge-in while it's mid-sentence.
3. Status block updated: per-step PASS/FAIL, side-by-side with the Chrome
   baseline from step one.
