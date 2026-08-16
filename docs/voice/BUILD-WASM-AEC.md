# Demo 3 — browser voice agent with WASM echo cancellation

> **Status (2026-08-16): built + desktop-verified; iPhone spike handed to
> Austin's phone, results pending.** Repo:
> **github.com/clawdbotatg/clawd-wasm-gpt-voice** (fork of gpt-voice; runs on
> ports 8127/8447 on the Mac — 8123 was already taken by the original
> gpt-voice). `spike.html` is the spike; results auto-POST to `/report` →
> `spike-results.jsonl`.
>
> - **Canceller (numeric): PASS.** speexdsp mdf+preprocess → standalone wasm
>   (67KB, emscripten, no JS glue, 4 WASI stubs). `node aec/test_aec.mjs`:
>   ~68dB ERLE settled on a simulated 12ms/-6dB echo at 48kHz, 200ms tail.
>   Trap found: a pure stacked-sine far end makes MDF converge then mistrack
>   (40dB → 10dB) — test with speech-shaped noise, which is also what real
>   speech does.
> - **Spike test 0 (synthetic pipeline, added): PASS on desktop Chrome** —
>   real worklet + wasm vs a simulated 50ms/-6dB echo inside the graph, ~44dB
>   settled. No mic/speaker needed; run it first on every device.
> - **Spike test 1 (remote-track tap): PASS on desktop Chrome, with a
>   mandatory workaround** — the plain `MediaStreamAudioSourceNode` tap of a
>   remote WebRTC track reads all-zero **even on desktop Chrome**; attaching
>   the stream to an `<audio>` element (muted is fine) makes the tap live
>   (peakRMS 0.36). The demo therefore keeps the remote stream on its audio
>   element (it's also the speaker output). iPhone: pending.
> - **Spike test 2 (speaker loop through the canceller): not runnable on the
>   build Mac** — its CoreAudio is wedged system-wide (`AudioQueueStart
>   -66681` from afplay/say/Chrome; needs a sudo coreaudiod kick nobody has
>   the password for right now). iPhone: pending — that's the device that
>   matters.
> - **Demo integration: done** (`index.html` 🔇 toggle, default on): raw mic →
>   worklet → cleaned track to the peer connection, remote tapped as
>   reference, live ERLE readout, auto-fallback to browser AEC.
>   `tools/demoprobe.mjs` (stubbed token/SDP/WebRTC/mic) all green;
>   `tools/spikeprobe.mjs --no-audio-out` drives the spike page on machines
>   with broken audio out.
> - **README speaker loop test (the acceptance bar): pending the iPhone run.**
>   Austin was pinged on Telegram with https://192.168.68.61:8447/spike.html
>   (LAN HTTPS, self-signed) + the demo URL.
>
> Highest-risk demo of the three — it has an explicit kill criterion; a
> well-documented failure is a successful outcome.

## Goal

The gpt-voice browser demo, made full-duplex on a device speaker by running
**our own echo canceller in the page**: raw mic + a reference tap of the
assistant's audio into a WASM canceller inside an `AudioWorklet`, cleaned mic
out. If this works, natural GPT-style talking works on ANY device with no
native app — that's the prize that justifies the fiddliness.

Read `docs/voice/README.md` in clawd-harness first (context + the acceptance
test). **Fork github.com/clawdbotatg/gpt-voice as the starting point** — it is
the verified-working baseline (token mint, WebRTC session, semantic VAD); this
demo changes ONLY the audio path into the peer connection.

## The pipeline

```
mic getUserMedia (echoCancellation: FALSE — raw; two cancellers fight)
        │
        ▼                        remote WebRTC track (assistant voice)
  AudioWorkletNode  ◄── reference ── MediaStreamAudioSourceNode
  (speexdsp AEC in WASM, 48kHz mono, ring-buffered 128-sample quanta)
        │
        ▼
  MediaStreamAudioDestinationNode ─ .stream.getAudioTracks()[0] ─► pc.addTrack
```

- **Canceller**: `speexdsp`'s `speex_echo_*` (MDF adaptive filter) compiled
  with emscripten — small, battle-tested, permissive license, existing wasm
  builds to crib from. (libwebrtc's AEC3 is better but extracting it is a
  project; only consider it if speex gets close but not over the line.)
- **Framing**: speex wants fixed frames (10ms = 480 samples @48k); the worklet
  hands you 128-sample quanta — ring-buffer between them.
- **The hard part is delay alignment**: the reference is tapped pre-speaker;
  the echo reaches the mic tens of ms later (output buffering + acoustics,
  device-dependent). Start with a long filter tail (100–200ms) and let the
  adaptive filter absorb it; add a coarse cross-correlation delay search only
  if adaption won't converge.
- Keep `noiseSuppression`/`autoGainControl` ON in getUserMedia; only
  `echoCancellation` goes off.
- One `AudioContext` (48kHz explicit), created inside the click handler.
- The assistant stays audible via the existing `<audio>` element; the WebAudio
  tap is an additional consumer. If that double-plays on some browser, play
  through WebAudio instead and drop the element.

## Spike FIRST (the kill criterion)

Before touching the demo proper, build a 20-line test page and answer two
questions **on an iPhone** (the device that matters and the flakiest):

1. Does `MediaStreamAudioSourceNode` on a remote WebRTC track actually produce
   samples in iOS Safari? (Historically flaky. Silent tap = empty reference =
   the whole approach is dead on iOS.)
2. Play a looped voice clip out of the speaker while recording the mic through
   the canceller: is the echo audibly attenuated (record with/without and
   listen, or compute ERLE)?

If either fails on iOS: **stop**, write up exactly what failed in Status, and
note whether desktop browsers passed (a desktop-only pass is still worth
keeping as a flagged experiment).

## Definition of done

1. Spike results recorded (iPhone + desktop Chrome).
2. If the spike passed: the forked demo runs the full pipeline, and **the
   README speaker loop test passes on a phone speaker** with the WASM
   canceller as the only echo protection.
3. Status block updated with per-step PASS/FAIL per device.
