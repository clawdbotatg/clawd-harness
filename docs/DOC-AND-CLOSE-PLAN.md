# Plan 2 — 📑 wrap: document the handoff, then close yourself

Status: **planned 2026-09-03, not started.** Depends on
`SESSION-HISTORY-PLAN.md` (part 1): the archive is the net, the undo toast
is the escape hatch, and the archive row's summary is where the handoff's
TLDR ends up.

## What the button does

Today 📑 "doc" sends one line ("document everything as if you are handing
this knowledge off…") and leaves the session open; you then ✕ it by hand.
After this plan, tapping 📑 means **wrap**: the session writes the handoff,
commits it, and then closes *itself*, disappearing from the tab strip and
landing in 📄 history with its TLDR as the row summary.

The user's stated fear is sessions "closing themselves left and right". So
the whole design is one rule:

> **A session can only close itself while a human (or the PM, on a human's
> behalf) has armed it — and the arm is short-lived.**

An unarmed session that runs the close command gets a polite refusal it can
read. Nothing a session does on its own can arm it. And because part 1
archives every close, a wrong close is one tap from undone.

Decisions made: 📑 becomes wrap outright (doc-without-close is just typing
the sentence). The close is deferred to the end of the turn, never mid-tool.
The harness never force-closes a wrapping session — if claude doesn't call
the command, the session just stays open and the arm expires.

## Flow

```
tap 📑  ──►  UI: hsend {type:"wrap", cid, text}
             server: arm(cid)  +  deliver `text` as a normal send (via:"wrap")
claude:      writes HANDOFF, commits/pushes, runs `harness-close`
             ──► POST /self/close?t=…&cid=…   (gates below)
             ◄── 200 "closing when this turn ends"
             ends its turn with a 3-line TLDR
Stop hook    ──► last_answer = TLDR  →  MGR.close(cid, reason="self")
             ──► archive row (part 1)  →  tab vanishes  →  toast "wrapped · undo"
```

## Server (`server.py`)

### The command the session runs
- Commit a tiny POSIX script **`bin/harness-close`** (new dir, only this
  file; nothing else goes on the child's PATH):
  ```sh
  #!/bin/sh
  # Ask the harness to close this session when the current turn ends.
  # Refused unless a human armed it with 📑 — see docs/DOC-AND-CLOSE-PLAN.md.
  [ -n "$HARNESS_SELF" ] || { echo "harness-close: not running under the harness"; exit 2; }
  curl -fsS -m 5 -X POST "$HARNESS_SELF/close" --data-urlencode "reason=$*"
  ```
  It prints the server's plain-text reply, so claude sees "closing…" or the
  refusal in its tool output. No token in the file (gitleaks stays green).
- Child env, in the engine-common `env()` block next to `TERM`/`COLUMNS`:
  `HARNESS_SELF = http://127.0.0.1:{PORT}/self?t={TOKEN}&cid={cid}` and
  `PATH = {HERE}/bin:{PATH}`. Works for codex too (same env plumbing as
  `HARNESS_CID`). `SCRUB_ENV` is untouched.

### Endpoint `POST /self/close` (in `do_POST`, next to `/hook`)
Plain-text replies; each is a sentence claude can act on. Gates, in order:
1. token valid and cid is a live session → else `404 no such session`.
2. **armed**: `s.wrap_armed_at` within `WRAP_TTL_S = 1800` and
   `s.wrap_turns_left > 0` → else `403 self-close is not armed. Only the
   human's 📑 wrap button (or the PM's wrap verb) can arm it — tell the human
   you're done and stop.` **This is the guard.**
3. not `s.ceremony`; not `s.autopilot` (the supervisor owns that session) →
   `409`.
4. **clean tree**: if the project path has a `.git` and
   `git status --porcelain` (5 s timeout) is non-empty → `409 worktree has
   uncommitted changes — commit (or stash) first, then run harness-close
   again.` A dirty tree in the self project also blocks that box's
   auto-pull, so this gate is "keeping things clean" made literal.
5. Accept: `s.wrap_closing = True`, reply `200 closing when this turn ends
   — finish with a 3-line TLDR.` Start a `WRAP_GRACE_S = 20` fallback timer
   in case no Stop hook ever arrives (e.g. the session is killed by claude
   itself).

### Arm / disarm
- `wrap(cid, text=None)`: `wrap_armed_at = now`, `wrap_turns_left = 2`
  (the doc turn plus one follow-up: a "yes, go" or an answered question),
  then deliver `text or WRAP_PROMPT` through the normal send path with
  `via:"wrap"` (so `.clawd-harness.prompts.jsonl` records it like any send).
- `wrap_cancel(cid)`: clear both. Also cleared by `wrap_turns_left` hitting 0
  or TTL expiry — silently; the session stays open and the badge goes away.
- In `on_hook`'s `Stop` branch, after `last_answer` is set: if
  `wrap_closing` → `self.manager.close(self.cid, reason="self")` (on a thread,
  like the other Stop-time work). Else if armed → `wrap_turns_left -= 1`.
- `meta()` gains `wrapArmed: bool` and `wrapClosing: bool` (rides the
  `sessions` broadcast; the UI badge and banner key off it). Not persisted —
  a restart disarms, which is the safe direction.

### `WRAP_PROMPT` (server-owned default; the UI chip carries the same text)
> We're wrapping this session up. Write the handoff for another agent or a
> future you: what changed, what's shipped vs. still local, open threads,
> gotchas, and the exact next steps. Put it where this project keeps such
> notes (an existing HANDOFF / HISTORY / docs file; else `HANDOFF.md` at the
> repo root). If this is a git repo with a remote, commit and push it.
> Then run `harness-close` — it closes this session once the turn ends.
> If something is unresolved or you need a decision from me, do NOT close:
> say what's open and stop. End your last message with a 3-line TLDR.

The trailing TLDR is what the 📄 history row shows (via `last_answer`).
`test_resume_gate.py`'s echo-trap rule applies: the prompt must never quote
the resume-modal option list.

### WS frames (update `docs/WS-PROTOCOL.md`)
| frame | args | effect |
|---|---|---|
| `wrap` | `cid`, `text?` | arm + deliver the wrap prompt. |
| `wrapCancel` | `cid` | disarm; nothing else. |

Fleet: verbatim forwarding, nothing relay-side. `/self/close` is hit
locally on the box that owns the session, so it never crosses the relay.

## UI (`index.html`)
- The 📑 entry in `QUICK_PROMPTS` gets `wrap: true`; `sendQuick` sends
  `{type:"wrap", cid, text}` (via `hsendTo(machine,…)` in fleet mode) and
  still does `addPending(text)` so the composer echo and 🕘 history behave
  like any chip. Label stays "doc" — that's the muscle memory; the hover tip
  explains it now closes when done.
- **Armed state is visible.** The tab / rail row shows a 📑 badge while
  `wrapArmed`. Above the composer, the existing 🤖 pilot-status row pattern
  hosts a thin line: "📑 wrapping up — closes itself once the handoff is
  written · cancel". `cancel` → `wrapCancel`. While `wrapClosing`, the line
  reads "📑 closing when this turn ends".
- **When it vanishes under you**: the `sessions` frame drops the cid. Reuse
  `closeCurrentSession()`'s landing logic (rail neighbour, else the
  projects rung) and show the part-1 toast: "📑 <title> wrapped up · undo".
  Undo = `reopen`.
- 📄 history row for `closed_by:"self"` shows the 📑 tag, and its expanded
  summary is the TLDR.
- Touch: everything above is tappable at natural pace; no confirm modal
  (the arm is cancellable for the whole time the session is writing, and
  undo exists after).

## Controller — three places together
- verb `wrap(machine, cid, text=None, confirm=False)`, gated like `close`.
  MCP description: "Ask a finished session to write its handoff and close
  itself; prefer this over `close` when the work is done."
- persona: when a session is done, wrap it instead of closing it; never
  wrap a session that is blocked on a question.
- `docs/CONTROLLER.md`.

## Tests / probes / gate
- `test_wrap.py` on an isolated **copy** of `server.py`: unarmed `/self/close`
  → 403 and the session is untouched; armed but dirty temp git repo → 409;
  armed + clean → 200, `wrap_closing`, then a synthetic Stop hook closes it
  with `reason:"self"` and the archive row's `last_answer` is the Stop
  payload; grace timer closes without a Stop; two Stops without a close →
  disarmed, session alive; TTL expiry disarms; `wrapCancel`; autopilot and
  ceremony refusals; the script exits 2 without `HARNESS_SELF`.
- `tools/wrapprobe.mjs` (fake sessions, stubbed `hsend`, emulated touch,
  real taps): tapping 📑 sends `wrap` and the banner appears; tapping cancel
  sends `wrapCancel`; a `sessions` frame without the current cid lands on
  the neighbour and shows the undo toast; undo sends `reopen`. Screenshot —
  look at it.
- gitleaks pre-commit: `bin/harness-close` carries no secret.
- `tools/checkall.sh` green, then `shipcheck --wait`.

## Docs
`docs/WS-PROTOCOL.md`, `docs/CONTROLLER.md`, `docs/HISTORY.md`, `README.md`
(📑 now wraps), `CLAUDE.md` landmine: **"self-close is armed-only —
`/self/close` refuses unless 📑/`wrap` armed it within the TTL and turn
budget; never widen that gate, never force-close on the harness side."**

## Order of work (after part 1 is shipped)
1. `bin/harness-close`, env planting, `/self/close` with all gates,
   arm/cancel/Stop-time close, `meta()` fields, `test_wrap.py`. Gate green.
2. UI: chip → `wrap`, badge + banner + cancel, vanish landing + undo toast,
   `wrapprobe`. Gate green.
3. Try it for real on one throwaway session in the self project; check the
   archive row and the undo.
4. Controller verb + MCP + persona. Docs. `checkall`, `shipcheck --wait`.

Rough size: server ~140 lines, script ~6, UI ~90, tests/probe ~180, docs ~50.
