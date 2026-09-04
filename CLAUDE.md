# clawd-harness — orientation for Claude

A web harness for driving interactive (subscription-billed) Claude Code
sessions from a browser. `README.md` is the user-facing overview. This file is
the **operative rulebook only** — the full feature-by-feature history, with
dates, evidence, and war stories, lives in **[`docs/HISTORY.md`](docs/HISTORY.md)**
(read it when you need the *why* behind a rule; don't re-litigate a rule just
because the story isn't inline here).

## Definition of done

1. **`tools/checkall.sh` green** — discovers and runs every `test_*.py`
   (root + fleet) and every probe in `tools/`. Run it before any push that
   touches `index.html`, `server.py`, or `fleet/`. When you add a feature,
   add/extend a guard; the gate picks it up automatically.
2. **`python3 tools/shipcheck.py --wait` exits 0** — tree clean, HEAD pushed,
   `h.atg.link` serving HEAD's `index.html` byte-for-byte.

**Push to main IS the deploy.** Every box self-updates (~5 min; the relay box
~3 min). The standing "only commit when asked" default does not apply here.
The trap: saving `index.html` hot-reloads browsers on *this* box in ~1s and
`uiprobe` screenshots the working tree — every local signal says "shipped"
while production still serves the last push. Never call a UI change live on
local evidence; finish with shipcheck. A dirty worktree also *disables this
box's auto-pull*, silently blocking everyone else's deploys from landing here.

## Run / test

- `python3 server.py` → tokenized URL on **port 8787** (token in
  `.clawd-harness.token`). It's usually already running under launchd
  (`com.clawd.harness`, KeepAlive) — check with
  `launchctl list | grep clawd.harness`, not pgrep.
- Saving `index.html` live-reloads local browsers; editing `server.py` or
  `.clawd-harness.env` triggers a graceful self-restart that waits for
  mid-turn sessions (banner + `restart now` button + 20 min ceiling).
- Verify JS edits: extract the `<script>` and `node --check` it.
- **Probes (`tools/*.mjs`)** drive the app in local headless Chromium — the
  claude-in-chrome MCP browser is remote and cannot reach 127.0.0.1. Probes
  must never touch a real session: use fake sessions + stubbed `hsend`/
  WebSocket (splashprobe pattern), and probe **user gestures** (real taps at
  natural pace on emulated touch), not `element.click()` — three production
  bugs were invisible to synthetic clicks.
- **Never test `server.py` from this directory copy-free** — it will resume
  the real sessions. Copy it to an isolated dir first.

## Architecture (the 60-second version)

- **server.py** — one `SessionManager`, N projects (git repos under
  `projects/`, gitignored), N sessions (each an interactive `claude` in a PTY;
  `codex` is the second engine behind the `Engine` strategy object). `cid` is
  our stable id; claude's `session_id` rotates. Registry:
  `.clawd-harness.sessions.json`, `--resume`d on restart. Disk is truth for
  the project list (reconcile loop). Project kinds: gh (amber) / local
  (violet, path never leaves the machine) / external (teal, fork-and-PR with a
  standing rule). Secrets load from gitignored `.clawd-harness.env`.
- **Channels:** WRITE = keystrokes → PTY; READ = raw PTY bytes → xterm.js, and
  transcript JSONL → slim events. We never parse terminal text — except the
  three sanctioned PTY tripwires (limit banner, resume gate, onboarding).
- **Accounts/routing:** N subscription logins (`~/.clawd-accounts/<name>`),
  usage-polled; sessions spawn on the coolest pool and get rescued/handed off
  when a plan walls. The contract is **`EXPECTATIONS.md`** — read it first on
  any "my sub broke" report. Everything in the router is fenced behind
  `Engine.routes_accounts` (claude-only). Deep doc:
  `docs/fleet/SUB-ROUTING.md`.
- **index.html** — the single UI file, one page, hash routing
  (`#/` projects · `#/p/<pid>` sessions · `…/s/<cid>/tty` terminal ·
  `#/pins` · `#/irons`, `#/i/<id>`). Served untouched in direct mode; the
  fleet relay injects `window.__FLEET__`. **One copy — edit here, push.**
- **fleet/** — relay + worker driving N harnesses from one phone. Hard
  boundary: fleet code never imports `server.py`; the wire contract is
  `docs/WS-PROTOCOL.md` — keep it in sync with any WS change. Deep docs:
  `fleet/CLAUDE.md`, `docs/fleet/` (ADD-MACHINE.md for new boxes).
- **controller/** — the PM, a WS client like any other; verbs in
  `controller/verbs.py`. **A harness feature doesn't exist to the PM until you
  update three places together: the verb, its MCP description, and the persona**
  (`controller/prompts/private.md`). Deep doc: `docs/CONTROLLER.md`.
- Feature docs on demand: `docs/CODEX-ENGINE.md`, `docs/DEEPLINKS.md`,
  `docs/fleet/ACCOUNTS-PANEL.md`, voice in `docs/CONTROLLER.md` + `docs/voice/`,
  `docs/fleet/SKILLS.md` (the private skill library on the relay: 📚 picker
  pastes a skill's text into a session; `skillput` publishes; no machine
  installs). **🟦 Live TLDR** = the `API_TEE` block in `server.py`: every
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
  restarts and handoffs don't wipe them. `test_tldr.py` + `tldrprobe.mjs`.
  Wall-display boxes (clawd-sat): `tools/kiosk/README.md`.

## Landmines (don't regress; stories in HISTORY.md)

1. **`SCRUB_ENV`** — scrub `CLAUDECODE`/`CLAUDE_CODE_*`/`ANTHROPIC_API_KEY`
   from child env, or nested claude runs embedded (no transcript, metered
   billing).
2. **`SEND_SETTLE` + bracketed paste** — pause between text and `\r`; large
   sends ride as bracketed paste or the TUI drops the head. A "my text got cut
   off" report is a *display* artifact until proven otherwise: diff
   `.clawd-harness.prompts.jsonl` (full send log) against what claude did.
   Harness-typed slash commands need a trailing space (`/compact `) or the
   picker eats the CR.
3. **`CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1`** in child env — alt screen
   silently kills all scrollback. (CLI behavior can flip server-side per
   account with no local change; grep the CLI bundle when something changes
   by itself.)
4. **Repaint, don't rebuild** — any list that repaints on server frames
   (projects rung, tab strip, iron row, iron list) must reconcile nodes by id,
   never `innerHTML=''`: frames land several times a second and a wipe eats
   scroll position, focus, un-mirrored input text, and the very card under a
   finger mid-tap. Only arrival may focus a filter box; never refill an input
   from its JS mirror. The pin board is the one sanctioned wholesale-rebuild
   exception. Same law for dictation: STT results write into the composer only
   while the box still holds dictation's own last write in the same draft
   context (`recWrote`/`recCtx`); typing and tab switches always win
   (`tools/sttprobe.mjs` enforces).
5. **Shared-PTY sizing is ownership-based** (`claim_resize`) — deliberate acts
   claim the geometry; never regress to last-resize-wins. Respawns carry
   geometry + viewers via `clone_for_respawn`/`adopt_viewers` — never
   hand-copy session fields on respawn (every hand-copied list has eventually
   dropped one; ctor params + registry rows or it doesn't survive).
6. **No file in this repo may quote the resume-modal option list verbatim**
   (a session replaying it would trip the PTY scan — the echo trap;
   `test_resume_gate.py` enforces).
7. **Iron scope is entered only through iron surfaces** — the list, a `#/i/`
   link, a project card's 🔥 badge, or the picker (Ctrl+Space /
   Enter-on-lone-match); normal session navigation never auto-enters an iron.
   Opening an iron dives into its warmest session, waiting for session frames
   rather than mislabeling the iron empty; only a **sessionless** iron lands
   on the iron page (`#ironview` — member project rows, each with a ＋ that
   spawns a session *inside* the iron; the page doubles as the dive-wait's
   waiting room). The irons LIST has one combined create/filter box (never add
   a second form there); the picker's create-and-assign is the one other
   sanctioned create path.
8. **Terminal is read-only on touch, always** — dictation through the
   composer, TUI menus through the key bar.
9. **Never commit** runtime/secret files (`.clawd-harness.*`, uploads,
   `projects/`, `tools/*.png`); `share/` must stay credential-free (this repo
   is public; a gitleaks pre-commit hook also runs). Git identity:
   **clawdbotatg** / `clawd@buidlguidl.com`, HTTPS.
10. **Search this repo with `rg`.** If any grep comes back suspiciously
    empty, suspect a raw control byte in the file before concluding the code
    isn't there.

## Periodic

- `python3 bench_naming.py` — re-benchmark the naming model (`BANKR_MODEL`)
  ≈ quarterly; next ≈ 2026-09.
- `python3 tools/mine_quick_prompts.py` — re-mine the composer quick-chips
  ranking ≈ quarterly; next ≈ 2026-11.
