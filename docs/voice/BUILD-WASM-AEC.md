# Build 3 — software echo cancellation in the browser (WASM)

> **Status: not started.** Owner: (assign an agent). Record the speaker loop
> test result here when done. This is the highest-risk, lowest-confidence
> build of the three — treat it as an experiment with a kill criterion, not a
> product commitment.

## The bet

If we run our own acoustic echo canceller in the page — mic signal + a
reference tap of the assistant's audio in, cleaned mic out — then full-duplex
works on ANY device with no native app at all: phone Safari, random Android,
someone else's laptop. The browser gives us all the pieces: WebAudio can tap
the remote WebRTC track (the exact echo reference), an `AudioWorklet` can run
a WASM canceller per 128-sample block, and the cleaned output can go back into
the peer connection via `MediaStreamAudioDestinationNode` + `replaceTrack`.

Read `docs/voice/README.md` first (problem statement + the acceptance test).

## Where it plugs in

All client voice code lives in one block of `index.html` in clawd-harness
(search `pmVoiceStart`). The integration point is exactly one line: the track
handed to `pc.addTrack(...)`. Everything else (session mint, tools, HUD,
half-duplex fallback) stays untouched. Gate the experiment behind a query flag
(e.g. `#/pm?aec=wasm` or a localStorage flag) so it can ship dark and be
A/B'd against the built-in path on the same device. Guards you must keep
green if you touch clawd-harness: `tools/voiceprobe.mjs`, `node --check` on
the extracted script, `python3 tools/shipcheck.py` before claiming it's live
(read the repo CLAUDE.md "Definition of done").

## The pipeline

```
mic getUserMedia (echoCancellation: FALSE — raw, no double-AEC)
        │
        ▼                       remote WebRTC track (assistant voice)
  AudioWorkletNode  ◄─── reference ─── MediaStreamAudioSourceNode
  (WASM AEC, 48kHz mono, 128-sample quanta)
        │
        ▼
  MediaStreamAudioDestinationNode ── .stream.getAudioTracks()[0] ──► pc.addTrack
```

- **Canceller**: `speexdsp`'s `speex_echo_*` (MDF adaptive filter) compiled to
  WASM is the practical choice — small, battle-tested, permissively licensed,
  existing emscripten builds to crib from. libwebrtc's AEC3 is better but
  extracting it is a project in itself; only go there if speex gets close but
  not over the line.
- **Frame plumbing**: run the canceller inside the `AudioWorkletProcessor`.
  Two inputs (mic, reference), one output. Speex wants fixed frames (e.g.
  10ms = 480 samples at 48k): ring-buffer the 128-sample quanta in/out.
- **The hard part is delay alignment.** The reference you tap is *pre*-speaker;
  the echo arrives at the mic after output buffering + acoustics (tens of ms,
  device-dependent, and `AudioContext.outputLatency` only tells you part of
  it). Speex's filter tail (`filter_length`, use 100–200ms) absorbs moderate
  misalignment — start there before building explicit delay estimation.
  If adaption never converges, add a coarse delay search (cross-correlate
  reference vs mic over the first seconds of assistant speech).

## Known traps

- One `AudioContext` for everything, created in the click handler (autoplay
  policy), `sampleRate: 48000` explicit.
- iOS Safari: `AudioWorklet` works (14.5+), but verify the remote-track-into-
  WebAudio tap actually produces samples on iOS — historically flaky; if the
  tap is silent, the reference is empty and the canceller no-ops. This single
  check decides whether the build is viable on the device that matters most —
  do it FIRST (a 20-line spike page).
- Keep the assistant audible: the `<audio>` element keeps playing the remote
  track as today; the WebAudio tap is an additional consumer, not a
  replacement. (If double-audio appears, play through WebAudio instead and
  drop the element.)
- Turn the browser's own AEC OFF on the mic for this path — two cancellers
  fight each other — but keep `noiseSuppression`/`autoGainControl` on.
- CPU: speex MDF at 48k mono is light (< a few % on a phone), but confirm no
  worklet underruns (they surface as crackle).

## Definition of done / kill criterion

1. Spike first (before any integration): a standalone test page that plays a
   looped voice clip through the speaker while recording the mic through the
   canceller, and reports echo attenuation (ERLE, or just: record with/without
   and listen). **On an iPhone.** If the remote-track tap or the attenuation
   is hopeless here, STOP and write up why — that's a successful experiment.
2. If the spike passes: integrate behind the flag, then run the README
   speaker loop test on a phone with 🎧 mode ON (i.e., half-duplex disabled,
   our canceller the only protection).
3. PASS = the loop test holds. Partial pass (works on desktop, not iOS) is
   worth shipping dark for desktop non-Chrome browsers — note it in Status.
