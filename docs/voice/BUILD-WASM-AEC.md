# Demo 3 — browser voice agent with WASM echo cancellation

> **Status: not started.** Record the spike + speaker loop test results
> (README.md) here when done. Highest-risk demo of the three — it has an
> explicit kill criterion; a well-documented failure is a successful outcome.

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
