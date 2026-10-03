# Feature notes (moved out of CLAUDE.md, 10-03)

Read the section for the feature you're touching. Not loaded every session.

- Feature docs on demand: `docs/CODEX-ENGINE.md`, `docs/DEEPLINKS.md`,
  `docs/fleet/ACCOUNTS-PANEL.md`, `docs/fleet/DOCS-STORE.md` (the shared
  shelf: `/docs/*` on the relay, own token, the `fleet-docs` library skill), voice in `docs/CONTROLLER.md` + `docs/voice/`,
  `docs/fleet/SKILLS.md` (the private skill library on the relay: a 📚 tap
  ATTACHES the skill to your next message as a chip, like a dropped `.md`,
  and Enter sends text + a one-line pointer claude Reads; `skillput`
  publishes; no machine installs; credentials belong in skills — audited,
  see the doc; it's the handoff doc for the whole feature). **🟦 Live TLDR** = the `API_TEE` block in `server.py`: every
  claude session's `ANTHROPIC_BASE_URL` points at a local pass-through proxy
  that tees the streamed reply to a rolling `claude -p haiku` summary (the
  blue block over the terminal). Pure pass-through, never logs, must never
  break a session; `claude -p` bills the subscription (the June-15 credit
  pool was paused — don't "fix" that). **🔊 the voice** = `voice_pick` in the
  same block: with 🟦 + 🔊 on, the summary is READ ALOUD as it solidifies —
  each sentence once it survives a pass unchanged (settled = solid on
  screen), the rest at the final pass, a reply too short for a summary as
  is (`say` frames). What you see is what you hear; no separate model
  decides what to say (that third loop existed for a few hours and was
  removed). Summary + both flags persist (ctor params + registry) so
  restarts and handoffs don't wipe them. `test_tldr.py` + `tldrprobe.mjs`;
  the handoff doc is **`docs/TLDR-VOICE.md`** (data flow, every knob, the
  client landmines, `tools/tldr_e2e.py` for a real isolated run,
  `tools/tldrgeom.mjs` to measure a live session's overlay).
  **🎤 dictation** = two recognizers behind the mic TAP (tap on; tap again,
  Enter or ➤ stop it — a send waits ≤ `REC_TAIL_MS` for the last words) and
  the desktop space-hold: 🎯
  Deepgram (the page's OWN socket to api.deepgram.com, nova-3 + `keyterm`s =
  the ⚙️ word list + harness names + every project name — this is what makes
  it hear Codex/ethskills; creds per box via WS `stt`, `DEEPGRAM_API_KEY` in
  `.clawd-harness.env`, JWT when the key can mint else the key itself) and
  the browser's Web Speech fallback (live, no vocabulary). Both write through
  one guard (`recApply`): typing and tab switches always win. The engine is
  decided AT THE PRESS from creds in hand — never wait on the network to light
  the mic. The ⚙️ word list is SHARED: one file on the relay (`/stt/words`,
  `stt-words.txt` on the doc shelf, `docs/fleet/DOCS-STORE.md`) read by the
  page, the Mac tool and the iPhone keyboard — those two live in
  **`clawdbotatg/clawd-dictate`** (`~/clawd/clawd-dictate`; work on them
  THERE, its CLAUDE.md + `docs/HANDOFF-2026-09-15.md` carry the story).
  `test_stt.py` + `fleet/test_stt_words.py` + `tools/sttprobe.mjs`.
  Wall-display boxes (clawd-sat): `tools/kiosk/README.md`. **⑂ fork** =
  `SessionManager.fork` → `create_session(resume=<source id>, fork=True)` →
  claude's own `--resume <id> --fork-session` (new id + transcript, source
  untouched); the `fork` flag is a ctor param + registry field that clears on
  the first id rotation. `test_fork.py`; WS verb `fork` in `docs/WS-PROTOCOL.md`.
  **🔍 double-check** = `SessionManager.check` → the source writes a LOCAL
  `REVIEW-<stamp>.md` (one per review; `REVIEW-*.md` excluded like
  `HANDOFF-*.md`) → the first Stop past that brief (`_check_on_stop`, keyed on
  `prompt_count`, volatile arm) → `check_spawn` opens the OTHER engine in
  the same project with the brief + `git diff <head_at_spawn>..HEAD` (every
  session records HEAD at spawn: ctor param + registry). Brief = claims,
  diff = truth, the reviewer never edits. **The loop closes:** the reviewer's
  first Stop (`_check_back_on_stop` → `check_back`) appends its verdict to
  the review file and prompts the SOURCE to act on it (`CHECK_ACT_PROMPT`:
  think critically, fix what's right, say why not). A review nobody acts on
  is a tab nobody reads (Austin, 09-14). **Severity gauge** (Austin, 10-03: pass
  seven still "found" six nits): the reviewer tags critical/major/minor/nit
  + a last `SEVERITY:` line → `parse_severity` → the source's durable
  `check_log` (dots on the 🔍 chip); a pass with no critical/major gets the
  "fix the cheap ones, no further pass" act prompt. The reviewer's `check_of` /
  `check_file` / `check_pending` are ctor params + registry fields. **One
  tab, not two** (Austin, 10-02): the reviewer never gets a tab while its
  source is open — it's a live terminal pane STACKED under the source's
  (`syncPeek`; stacked, never side by side: width changes re-wrap the TUI),
  streamed over the same socket by the `peek` verb (`_PeekClient`: base64
  `peekPty` JSON, its own size claim via `peekResize`). Pane ✕ closes the
  reviewer only; after the hand-back it closes itself and the pane goes.
  `test_check.py` + `test_peek.py` + `tools/checkprobe.mjs`; WS verbs
  `check`/`checkCancel`/`peek`/`peekResize`.
  **☑ iron to-do list** = one shared list PER IRON (what's still open across
  the whole effort; NOT the life list on todo.atg.link — an iron's eight
  follow-ups stay on the iron). Engine `fleet/todo_store.py` (item-level ops,
  never a whole-list write); owner = the relay in fleet mode
  (`.clawd-fleet.todos.json`, WS verb `todo`, `todos` snapshot right behind
  every `prefs`) / the registry in direct mode. UI: the ☑ button in the iron
  row toggles an OVERLAY over the tty (right column on desktop, bottom sheet
  on touch; never a split — a split is a PTY geometry claim), remembered per
  iron; the sessionless iron page shows it inline; tap the words → composer.
  Sessions write with **`bin/harness-todo`** (`HARNESS_TODO_URL` →
  `/self/todo` → the local irons, or on a fleet box the relay's
  `/todo/agent`, which folds the project's key against the irons'
  member keys via `fleet/projkey.py` — the one Python copy of `projectKey`,
  the worker uses it too); the `share/skills/iron-todo` skill (kit-installed
  on every box, re-synced on any `share/` pull) tells a session when to use
  it. Agent writes are opt-in: when asked, and the 📑
  wrap prompt says to check off / add what's still open. **🔥 on
  todo.atg.link** mirrors every iron's list (read + tick/add/rm, ops applied
  here, `via:"todo"`) through the relay's `/todo/bridge` — its OWN token,
  minted at relay boot into `fleet/.clawd-fleet.todo-bridge.token` (0600),
  which clawd-todo on the same box reads; still separate lists, items never
  move between them. `test_todo.py`,
  `fleet/test_todo_store.py`, `fleet/test_relay_todos.py`, `tools/todoprobe.mjs`.
