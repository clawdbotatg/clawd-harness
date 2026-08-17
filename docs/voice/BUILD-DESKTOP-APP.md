# Demo 2 — Mac voice agent (FaceTime's echo cancellation)

> **Status (2026-08-17): the app WORKS live (VP AEC on — it hears, answers,
> and barge-in fires) — but the automated speaker-loop test FAILED, twice,
> audibly, with the user in the room, and was hard-stopped. The 4-step
> PASS/FAIL tables this block was supposed to hold do not exist.** Honest
> account, so the next attempt doesn't repeat it:
>
> **What works** (github.com/clawdbotatg/clawd-mac-gpt-voice): after three
> real bugs were found and fixed on live hardware, the native app runs with
> voice processing enabled and holds a working conversation — speech
> detected, transcribed, answered, barge-in flush+cancel firing. The bugs,
> each of which cost a debugging round:
> 1. **VPIO init fails (-10875) if the graph touches `mainMixerNode`.**
>    Wire the source node **directly to `outputNode`** at the device's
>    native format (resample in the render callback). Symptom bundle: the
>    input tap reports a phantom **4-ch** format and `kAUInitialize` dies on
>    the output unit. Flaps with hidden coreaudiod state — passes can be
>    followed by fails with the identical binary.
> 2. **`AVAudioConverter` silently emits all-zero frames** (no error) when
>    fed the VP tap's 4-ch format. The API heard pure silence while every
>    local counter looked healthy. Fixed with a manual ch0 → mono 24k
>    downsample.
> 3. Mic level via the workaround path was ~10× too quiet for server VAD —
>    added `VOICEMAC_MIC_GAIN`.
>
> **Why the automated test cannot work as designed:** macOS VP AEC uses
> **all system output** as its echo reference, so `say` through the Mac
> speakers — the test's fake human — is cancelled as echo. The app is deaf
> to the test *by design of the very feature under test*. The workaround
> (play `say` through the Yeti X's own output jack, which VP doesn't
> reference) produced quiet, muffled audio: mishears ("Froid", "好"), filler
> answers, phantom speech-starts, no measurable barge latency. Run 2's
> "passes" (steps 1/3) are vacuous — the app heard nothing at all in run 1
> and garbage in run 2.
>
> **The process failure:** each run talks through the open speakers for
> ~8 minutes. That ran twice (plus a mid-run harness restart that re-ran
> it), while the user was at the machine, without a fresh warning of how
> long and how loud it would be. The user issued a hard stop; everything
> audible was killed. Also burned: ~2 hours on macOS mic permissions —
> the unlock-time TCC dialogs auto-denied everything, and Chrome holds a
> hidden mic deny that survives every visible Settings toggle
> (`tccutil reset Microphone com.google.Chrome`, run by the user, is the
> only fix — never done, so the **Chrome baseline never ran** either; one
> partial run before Chrome lost its debug port did reach 🟢 live and
> answered, so the web path is believed fine but is unmeasured).
>
> **How to actually finish:** the README's speaker loop test needs a
> **real human voice** (or a genuinely independent second speaker) — that's
> what it was written for. The app is one command away
> (`env $(grep '^OPENAI_API_KEY' ../gpt-voice/.env) VOICEMAC_AUTOSTART=1 ./.build/debug/VoiceMac`):
> talk to it, interrupt it, score the four steps by ear, then write the two
> tables. Do not resurrect the `say`-loop for the native app.

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
