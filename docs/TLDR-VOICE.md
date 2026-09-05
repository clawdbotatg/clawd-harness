# 🟦 Live TLDR + 🔊 the voice — the handoff doc

Written 2026-09-05 at the end of the day it was built, for the next agent (or
me) who has to touch this. `CLAUDE.md` has the rules; `docs/HISTORY.md` has
the war stories; this is the *how it works and how to work on it*.

## What it is, in one breath

With 🟦 on, a blue plain-English summary of the reply appears under the
thinking line while claude is still writing, updating every few seconds and
tightening at the end. With 🔊 also on, that blue text is read aloud sentence
by sentence as each sentence stops changing. What you see is what you hear.

## Data flow

```
claude (PTY)  ──ANTHROPIC_BASE_URL──▶  ApiTeeHandler :8791  ──HTTPS──▶  api.anthropic.com
                                          │ pass-through, streamed, never logged
                                          │ SseTextTap: text_delta of the MAIN call only
                                          ▼
                              ClaudeSession.tee_text()  → tldr_turn_text (this turn's prose)
                                          │ (only if tldr_on)
                                          ▼
                              RollingTldr  (one `claude -p --model haiku` in flight, newest wins)
                                          │ _tldr_call(text_so_far, prev_summary, final)
                                          ▼
                              _tldr_emit() → settle_sentences() → {"type":"tldr", text, final, sents}
                                          │ (only if voice_on)
                                          ▼
                              voice_pick(said, sents, final) → {"type":"say", text}  per settled sentence
                                          ▼
                              browser: showTldr() paints; sayVoice() speaks (ElevenLabs via `tts` verb, or browser voice)
```

* **The tee** (`API_TEE` block, `ApiTeeHandler`, `start_api_tee`): every claude
  session is spawned with `ANTHROPIC_BASE_URL=http://127.0.0.1:8791/s/<cid>`.
  The path prefix is how one proxy routes N sessions (the CLI preserves it).
  It forwards method/path/headers/body untouched except `Accept-Encoding:
  identity` (so the SSE is readable), streams chunked, and tees `text_delta`
  events of the **main conversation call** only (`tee_call_kind`): body ≥
  `API_TEE_MAIN_MIN` (15 KB) **and carrying a `tools` list** **and not** a
  subagent (`cc_is_subagent=true` in the billing header inside `system[0]`).
  Subagents ride the same URL and once got summarized as if they were the
  reply; the WebFetch tool's own page summarizer (no tools, 40 KB of page in
  the request) once produced a TLDR of an audit report during a tool-only
  turn. Size alone is not a discriminator. Any
  tee-side exception leaves the stream flowing. Bind failure → `API_TEE`
  flips off and sessions go direct. An operator-exported `ANTHROPIC_BASE_URL`
  wins and disables it — unless it is itself a tee URL (`tee_is_ours`), which
  happens when a harness is launched from inside a harness session.
* **Why the tee at all**: the transcript JSONL lands each text block *whole*
  (a 2 k-char reply is one line written when it finishes) and the PTY is
  never parsed. The API stream is the only clean live source.
* **The summarizer** (`RollingTldr`, `_tldr_call`, `TLDR_SYS`/`TLDR_FINAL`):
  one `claude -p --model haiku` at a time, fed the whole reply so far + the
  previous summary, told to keep what's still true and extend. Thinking is
  OFF (`MAX_THINKING_TOKENS=0`): haiku otherwise thinks 500–4000 hidden
  tokens before a 50-word summary (8–43 s/call; ~2–4 s without). Each pass
  gets an explicit word budget sized to the reply (`tldr_budget`: a sixth of
  its words live, an eighth final, clamped) — haiku ignores a flat cap.
  `tldr_tidy` trims a ragged last sentence on live passes and backstops a
  model that ran long. Runs under the session's account dir, never through
  the tee (`ANTHROPIC_BASE_URL` popped). **`claude -p` bills the
  subscription** — the June-15 metered pool was paused; the header note in
  server.py that said otherwise was wrong for months.
* **Annealing**: `settle_sentences(prev, text)` marks a sentence settled iff
  it appeared verbatim in the previous pass. Settled = solid on screen,
  forming = 70 %. Settled is also what the voice reads.
* **The voice** (`voice_pick`): settled sentences not yet read (near-twin
  aware, difflib > 0.6), all remaining ones at the final pass, a reply too
  short for a summary (`< TLDR_MIN` chars) read as is, a busy `Notification`
  message spoken at once with `urgent:true`. That is the whole decision.
  There is deliberately **no model** here any more (see "What we tried").
* **Per-session state** (`ClaudeSession`): `tldr_on`, `voice_on`,
  `tldr_text` are **ctor params + registry fields** — a daemon restart or a
  handoff respawn used to wipe the summary ("why is the tldr not showing up
  at all"). `tldr_turn_text`, `tldr_read_at` (✕ = read this far), `tldr_sents`
  (annealing baseline), `_voice_said` are per-turn and volatile.
  `tldr_turn_reset()` on `UserPromptSubmit`; `tldr_turn_done()` on `Stop`.

## Wire (docs/WS-PROTOCOL.md is canonical)

* client → server `{"type":"tldr","cid","on":bool}` · `{…,"mark":true}` ·
  `{…,"voice":bool}` · `{"type":"tts","id","text"}`
* server → subscribers `{"type":"tldr","cid","text","final","turn","sents":[[s,settled]…]}` ·
  `{"type":"say","cid","text","urgent","turn"}` · to the asker `{"type":"tts","id","audio"|"error"}`
* The browser re-sends `on` and `voice` right after **every** `subscribe`
  and on either toggle. Fleet passes all of these verbatim (worker/relay are
  frame-agnostic; `tts` audio rides base64 inside the E2E channel because
  the relay doesn't proxy `/tts`).

## Client (index.html) — the parts that bit us

* `#tldr` is an **overlay in `#left`**, never in the footer or `#term`
  layout. The footer has a `ResizeObserver` that refits the terminal; a refit
  is a PTY resize + claude redraw + reset-and-replay on every other viewer.
  A footer row that grew per pass made "multiple sessions reload over and
  over". Nothing about this feature may change the footer's or `#term`'s
  height.
* **Placement** (`positionTldr` → `tldrPlace`): the chrome = the
  `tldrChromeRows()` buffer lines ending at the last non-blank line
  (`tldrLastBuf`): status line ← `─ ❯ ─` ← every blank row up to the line
  above (one blank while running, two when done; a fixed 5 left a blank row
  showing). The box top is the **real rectangle** of the first covered row,
  measured on the frame after paint and verified a frame later — never
  `index × offsetHeight` (row heights are fractional; an integer cell drifts
  a whole row over a screenful).
* **The hold** (`tldrHold`, `ttyBottom`, `bottomJustifyTTY`): with no summary
  (or a summary shorter than the chrome), the chrome rows the box doesn't
  need are pushed out of sight *without a resize*: with scrollback the
  viewport is held that many rows (+ trailing blank cursor lines,
  `tldrTrailing`) above the bottom; without, `bottomJustifyTTY` translates as
  if those rows weren't there. `ttyAtBottom` counts held rows as "at the
  bottom". While holding, the grid is lifted by its overhang (a fit from a
  taller moment can clip the last row) plus `TLDR_HOLD_GAP` px of air when
  the last line is bare. Lifted while the session is `waiting` (prompts
  render exactly there).
* **Boot gate** (`uiBooted`): `positionTldr`/`tldrHold` read `currentView()`
  which touches consts declared ~1000 lines later; with the mode persisted
  on, a render during boot hit the TDZ and every h.atg.link load died at
  "connecting…". Nothing in this feature may read view state before
  `uiBooted = true`.
* Box: fixed by content; row-rounding slack splits above/below
  (`margin:auto 0` — `justify-content:flex-end` can't scroll); 16 px; text
  selectable; ✕ dismisses (+ `mark`). Send blanks it, typing keeps it, a
  reconnect to the same cid keeps it, switching cid blanks it.
* Speech: `sayVoice` → `speak` → ElevenLabs (`/tts` in direct mode,
  `ttsViaWs` in fleet) at `TTS_RATE` (1.0 — 1.25 was too fast), browser voice
  when a box has no key. Held while the mic is open (`heldSay`), cancelled by
  a new prompt (`stopSpeech`).

## Knobs

`API_TEE=0` · `API_TEE_PORT` (8791) · `API_TEE_UPSTREAM` · `API_TEE_MAIN_MIN`
· `TLDR_MODEL` (haiku) · `TLDR_MIN` (40 chars before the first pass; was 160 — short replies got read raw) ·
`TLDR_CTX` · `TLDR_TIMEOUT` · `ELEVENLABS_API_KEY` / `ELEVENLABS_VOICE_ID`
(per box, in `.clawd-harness.env`; code default is Austin's voice) — client:
`TTY_COVER_ROWS` (5, the usual; measured at runtime) · `TLDR_HOLD_GAP` (4) ·
`TTS_RATE` (1.0).

## How to test

* `python3 test_tldr.py` — pure pieces: path routing, main-call gate,
  subagent gate, SSE tap under any chunking, budget, tidy, the loop
  contract, sentence settling, `voice_pick`. In the gate.
* `cd tools && node tldrprobe.mjs` — the UI on a forced fractional row
  height: toggle, frames, annealing, hold/overlay geometry in both regimes,
  both chrome layouts, ✕/mark, send/switch semantics, `say` frames,
  fleet `tts` round trip + fallback, cold start with the mode persisted on.
  In the gate.
* `python3 tools/tldr_e2e.py [--voice] [--prompt …]` — the real thing in an
  **isolated copy** of the harness (own dir/ports/registry; spawns one real
  claude session on the subscription, one prompt, prints every `tldr`/`say`
  frame with timestamps, then closes it). This is how every server-side
  change today was verified. Never run server.py from the repo dir for
  this — it would resume the live sessions.
* `cd tools && node tldrgeom.mjs <pid> <cid>` — dumps a live session's buffer
  tail and the overlay geometry verdict (box top vs thinking-line bottom,
  etc.) from headless Chromium against the local :8787 server. Use it when a
  screenshot says the placement is off; don't guess from the picture.

## Debugging

Grep `~/Library/Logs/clawd-harness.log`:
* `[tee <cid>] 200 main call NNNB → N chars of prose` — one per main call;
  `(subagent, skipped)` / `(no tap)` tell you why nothing was summarized.
* `[voice <cid>] on|off` and `[voice <cid>] say: …` — the flag and every
  spoken line. No `on` line after a subscribe = the client didn't send it.
* `[restart] pending/all idle` next to a missing summary = the old wipe
  (should be gone now: `tldr_text` is in the registry — check the row).
* `[handoff …] sub2 → ef` storms = the router evacuating a hot pool; each
  respawn used to wipe the summary too.
* Nothing at all: is the session's claude process carrying
  `ANTHROPIC_BASE_URL`? (`ps -Eww -o command= -p <pid>`). Sessions spawned
  before the tee shipped run direct until they respawn.

## What we tried and threw away (don't re-try without reading HISTORY.md)

* Blue box over the top of the terminal → Austin wanted it under the
  thinking line.
* A growing row in the footer → PTY resize storm.
* A fixed-height footer slot → black hole under the text.
* A black cover + separate text row → "why the black space".
* `index × cell` placement → off by a row on real fonts.
* A fixed 5-row chrome → blank row under the done line.
* **A third model loop deciding what to say** (`VoiceAgent`, listener test,
  ears-first prompt, said-log, then "speak as it arrives", then a word budget
  of a tenth of the reply enforced in code). It mismatched the screen and
  talked too much or too little. Removed the same day: the voice reads the
  blue text.
* `--effort low` to speed haiku → no effect; `MAX_THINKING_TOKENS=0` is the
  lever. `--bare` → needs an API key, never use it.

## Open

* Five chrome rows is the *usual*; the measurement handles 5/6. A multi-line
  TUI input box (typed directly on desktop) would be covered partially.
* `TLDR_MIN` = 160 chars: shorter replies get no blue text (the voice reads
  them as is). Whether that's right is taste.
* Two sessions editing index.html at once happened today; keep this work in
  one session.
