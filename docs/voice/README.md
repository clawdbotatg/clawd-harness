# Voice PM — the full-duplex problem, and three builds to solve it

## Context (read first, it's short)

The harness has a working voice front-end to the fleet PM: the PM tab's **🎙
talk** button opens an OpenAI `gpt-realtime` session over WebRTC (semantic VAD,
tool calls against the `/pm` endpoints, `ask_pm` for real PM turns). Server
half: `controller/voice.py`; client: the "Voice PM" block in `index.html`;
design doc: the "Voice front-end" section of `docs/CONTROLLER.md`; the verified
base recipe: [clawdbotatg/gpt-voice](https://github.com/clawdbotatg/gpt-voice)
`INTEGRATION.md`.

**The problem:** on a device speaker, the model's own voice re-enters the mic.
Semantic VAD reads it as the user talking → it interrupts itself → answers
itself → feedback loop ("three voices, stacked chaos" — happened in prod
2026-08-16, twice). Browser echo cancellation (`getUserMedia
echoCancellation:true`) is best-effort: decent on desktop Chrome, weak-to-absent
on phone browsers. Native apps (ChatGPT's) fix this with **OS-level acoustic
echo cancellation (AEC)** that a web page cannot reach: `AVAudioSession
.voiceChat` on iOS, the CoreAudio VoiceProcessingIO unit on macOS.

**Current mitigation (shipped, works, but is a compromise):** half-duplex —
the mic track is hard-disabled while assistant audio actually plays
(`output_audio_buffer.started/stopped`), tap-the-HUD to interrupt, and a 🎧
toggle for full-duplex (default ON on desktop, OFF on touch). See
`tools/voiceprobe.mjs` for the exact contract.

**Why this matters:** the natural turn-taking — knowing when to talk, when to
wait, being interruptible mid-sentence — *is* the product. Wake words, STT, TTS
are all commodity. Full-duplex on a speaker is the bar.

## The three builds

Each is a self-contained experiment with its own handoff doc, sized for one
agent to build and verify independently. They are ordered by expected
payoff-per-effort.

| # | Build | Doc | Bet |
|---|-------|-----|-----|
| 1 | Native iPhone shell (Capacitor + `AVAudioSession.voiceChat`) | [BUILD-IOS-APP.md](BUILD-IOS-APP.md) | OS AEC makes the existing web UI full-duplex on the phone speaker |
| 2 | Native macOS voice companion (Swift + VoiceProcessingIO) | [BUILD-DESKTOP-APP.md](BUILD-DESKTOP-APP.md) | OS AEC on the Mac, always-on menu-bar PM |
| 3 | WASM software AEC in the browser | [BUILD-WASM-AEC.md](BUILD-WASM-AEC.md) | No native app at all — cancel the echo in JS/WASM |

**Do not build Electron for this** — Electron is Chromium, same AEC as the
browser, zero gain. A PWA likewise changes nothing.

## Shared acceptance test (all three builds)

The **speaker loop test**, on the target device, device speaker at a normal
volume, no headphones:

1. Start a voice session, ask a question with a long answer ("tell me about
   the fleet in detail").
2. While it is talking: **stay silent.** PASS = it finishes and goes back to
   listening. FAIL = it interrupts itself / new responses spawn from its own
   voice.
3. While it is talking: **interrupt it by voice** ("stop, different question").
   PASS = it stops within ~a second and takes your turn.
4. Ten-turn conversation. PASS = zero self-triggered responses.

A build that passes 1–4 on speaker has beaten the browser. Record results in
the build doc's Status section.

## Shared plumbing every build can reuse

- **Token mint**: `POST /pm/api/voice/token` → `{value, exec, ...}` (ephemeral
  OpenAI secret + tool→endpoint map). Reachable on the fleet origin
  (`https://h.atg.link`, passkey-gated) or from a locally-run controller
  (`python3 -m controller serve` → `http://127.0.0.1:8799/api/voice/token`).
  The real `OPENAI_API_KEY` must stay server-side.
- **Tools**: execute against `/pm/api/tool` (POST `{name, args}`), `ask_pm` →
  `/pm/api/chat` (POST `{message}`, takes 1–3 min), lore → `/pm/api/voice/lore`.
  After every tool result send BOTH `conversation.item.create`
  (`function_call_output`) and `response.create` on the data channel, or the
  model never speaks the answer.
- **Session config** is minted server-side (`controller/voice.py`) — persona,
  semantic VAD, transcription, tool defs. Native clients get all of it for free
  by using the minted secret.
