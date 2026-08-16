# Voice demos — does GPT-style full-duplex talking work in each environment?

## What these are

Three **standalone demos**. Not harness features, not products — each is the
smallest possible "voice agent I can talk to" (OpenAI `gpt-realtime`: semantic
VAD, barge-in, natural turn-taking) built in a different environment, to answer
one question per environment: **does the smart turn-taking survive a real
device speaker there?**

The thing being tested is the turn-taking itself — it knows when to talk, when
to wait through your pauses, and stops when you interrupt. That magic dies the
moment the model hears its own voice out of the speaker (it interrupts itself
and loops — reproduced in prod 2026-08-16). Browsers can't reach the OS-level
echo cancellation native apps get, so: three environments, three echo
strategies, may the best one win.

| # | Demo | Echo strategy | Doc |
|---|------|---------------|-----|
| 1 | iPhone app | iOS `AVAudioSession.voiceChat` (OS/hardware AEC) | [BUILD-IOS-APP.md](BUILD-IOS-APP.md) |
| 2 | Mac app | CoreAudio VoiceProcessingIO (FaceTime's AEC) | [BUILD-DESKTOP-APP.md](BUILD-DESKTOP-APP.md) |
| 3 | Chrome + WASM | our own canceller (speexdsp WASM in an AudioWorklet) | [BUILD-WASM-AEC.md](BUILD-WASM-AEC.md) |

Each demo is a separate repo, buildable by one agent with no knowledge of the
others. Keep them TINY: a start/stop button, a state indicator, transcripts if
cheap. No tools beyond maybe `flip_coin`, no auth, no persistence, no design
pass. The demo exists to be talked to for ten minutes and judged.

## The canonical working reference

**github.com/clawdbotatg/gpt-voice** — a verified-working browser
implementation (~150 lines Python server, ~250 HTML/JS): token minting, WebRTC
session, semantic VAD, tool round-trip. Its `INTEGRATION.md` documents the
exact API shapes and the traps (both-events-after-a-tool, transcription is
opt-in, token is top-level `value`, realtime-only voice names). **Every demo
should crib from it** — it is the known-good baseline these demos vary from.

## Shared rules

- **The key**: `OPENAI_API_KEY` from the environment, minted into ephemeral
  client secrets by a local token server (gpt-voice's `serve.py` already does
  this — reuse it). Never bake the real key into an app binary or a web page.
  For on-device iPhone testing, `serve.py` on the Mac + its `ensure_cert()`
  self-signed-HTTPS-on-LAN trick is the proven path.
- **Session config**: `gpt-realtime` (or `-mini` for cheap iteration), voice
  `marin`, `semantic_vad` + `eagerness: auto`, input transcription on. Same
  shape as gpt-voice's `serve.py`.
- **Cost**: ~$0.06–0.11/min full model, ~1/3 that for mini. A demo session is
  pennies; don't engineer around cost.

## The acceptance test (same for all three)

**The speaker loop test** — on the target device, built-in speaker, normal
volume, NO headphones:

1. Ask a question with a long answer ("explain how sourdough works in detail").
   Stay silent while it talks. **PASS = it finishes and returns to listening;
   FAIL = it reacts to its own voice (interrupts itself, spawns responses).**
2. Interrupt it mid-answer by voice ("stop — different question"). PASS = it
   stops within ~a second and takes your turn.
3. Talk with natural pauses — trail off with "umm…" mid-sentence. PASS = it
   waits instead of jumping in.
4. Hold a ten-turn conversation. PASS = zero self-triggered turns.

Record PASS/FAIL per step in the build doc's Status block. A demo that passes
all four on speaker has matched the ChatGPT-app experience in that environment
— and tells us where the real voice product should live.
