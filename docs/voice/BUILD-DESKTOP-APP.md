# Build 2 — native macOS voice companion (Swift + VoiceProcessingIO)

> **Status: not started.** Owner: (assign an agent). Record the speaker loop
> test result here when done.

## The bet

macOS's CoreAudio **voice-processing I/O unit** (`kAudioUnitSubType_VoiceProcessingIO`,
or `AVAudioEngine` with `setVoiceProcessingEnabled(true)` on the input node)
gives the same OS-level echo cancellation FaceTime uses. A small native app
that does the *audio loop* natively — mic in through VPIO, assistant audio out
through the same engine — can be fully full-duplex on the Mac's open speakers.

Read `docs/voice/README.md` first (problem statement + the acceptance test).

**Important honesty note:** desktop Chrome's own AEC is decent (the web UI now
defaults 🎧 ON on desktop for exactly that reason). Before building, run the
README speaker loop test in Chrome on the target Mac. If Chrome already
passes, this build's value is not AEC — it's the always-on companion UX
(menu-bar, global hotkey, wake word later). Decide scope accordingly and note
the Chrome baseline result in Status.

## Shape of the build

- **New repo** (`clawdbotatg/clawd-voice-mac`). Swift, no Electron — Electron
  is Chromium and gains nothing (see README).
- **Menu-bar app** (LSUIElement), one window optional. v1 UI is: an icon, a
  start/stop item, a state word (LIVE/LISTENING/SPEAKING). The fleet's visual
  UI stays in the browser; this app is ears+mouth only.
- **Audio path**: `AVAudioEngine`, input node with
  `setVoiceProcessingEnabled(true)` (this turns on AEC/AGC/NS), output through
  the same engine. Capture PCM16 mono 24kHz for OpenAI; render the incoming
  audio deltas.
- **Transport — use WebSocket, not WebRTC.** Native WebRTC on macOS means
  vendoring the libwebrtc framework: heavy, slow to build, and unnecessary —
  the OpenAI Realtime API speaks the same events over WSS with base64 PCM
  audio (`input_audio_buffer.append`, `response.output_audio.delta`, ...).
  With VPIO doing AEC locally, the WebRTC stack buys nothing. One caveat to
  verify early: the `output_audio_buffer.*` events are WebRTC-only — over WS
  you track playback state yourself (you're rendering the audio, so you know).
- **Token + tools**: run the controller locally on the Mac —
  `python3 -m controller serve` in a clawd-harness checkout (it drives the
  local harness at `ws://127.0.0.1:8787` by default) — then:
  - mint: `POST http://127.0.0.1:8799/api/voice/token` → `{value, exec}`
  - connect WSS to OpenAI with `Authorization: Bearer <value>`
  - tool calls: dispatch per the `exec` map — `verb` → POST
    `http://127.0.0.1:8799/api/tool {name, args}`, `chat` → `/api/chat`
    (blocks 1–3 min; keep the audio loop alive meanwhile), `lore` →
    `/api/voice/lore?name=…`. Send BOTH `conversation.item.create`
    (function_call_output) and `response.create` after each result.
  No OpenAI key ever lives in the app.

## Semantic VAD over WS — verify, don't assume

The minted session config (built in `controller/voice.py`) carries
`semantic_vad` + transcription. Over WebSocket you must stream mic audio
continuously (`input_audio_buffer.append`) and let the server VAD commit turns.
Verify interruption behavior: on `input_audio_buffer.speech_started` while
playing, stop local playback and send `response.cancel` — that is barge-in,
and with VPIO's AEC the speech_started should only ever be the human.

## Definition of done

1. Menu-bar app runs on the Mac mini (clawd-heart), starts/stops a session.
2. **The speaker loop test in `README.md` passes on open Mac speakers** —
   including voice barge-in (step 3) — with the Chrome baseline recorded for
   comparison.
3. A tool round-trip works end-to-end by voice ("what needs me?" → sweep →
   spoken answer).
4. `ask_pm` works: hand it an order, keep chatting, hear the result when the
   PM turn lands.

## What NOT to do

- No Electron, no embedded browser (see README — zero AEC gain).
- Don't vendor libwebrtc unless the WS path measurably fails.
- Don't reimplement the persona/tools client-side — the mint response is the
  single source of truth; the app is a dumb, well-behaved audio terminal.
