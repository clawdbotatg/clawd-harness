# clawd-harness — orientation for Claude

A web harness for driving interactive Claude Code sessions from a browser.
`README.md` = overview. Why a rule exists: `docs/HISTORY.md`. Per-feature
notes (TLDR/voice, dictation, fork, 🔍 check, iron to-dos, skills): 
`docs/FEATURES.md` — read the part you're touching.

## Shipping (Austin, 10-03: "a lean shipping machine — not 10 minutes of tests for a 2 minute fix")

**Push to main IS the deploy.** Boxes self-update in ~3–5 min.

- **Small change:** edit, push, check it live. No `checkall`, no probe runs,
  no HISTORY.md / CLAUDE.md edits. If it touched one feature's test, run
  that one test (seconds), nothing more.
- **Big change** (new feature, fleet/ or passkey/E2E code, anything that
  broke before): the tests that cover it, then `tools/checkall.sh > file`
  (never `| tail` it — read the FAIL lines), then shipcheck.
- **Check it live:** `python3 tools/shipcheck.py` — UI bytes on h.atg.link
  match HEAD. Don't sit in `--wait` for fleet boxes; a box still restarting
  on busy sessions is normal. Fleet code changes: `--wait 2100` in the
  background.
- **Fleet pages don't reload on deploy.** After a UI push, tell Austin to
  hard-refresh. Local hot-reload and `uiprobe` show the working tree, not prod.
- **Config/env changes:** verify on the running process (`ps eww <pid>`,
  shipcheck's fleet table), not the file.
- A `server.py` change restarts a box only once committed and compiling
  (`_RestartGate`). Every restart kills + `--resume`s every session on that
  box — don't add restart triggers.
- A dirty worktree blocks this box's auto-pull. Commit or stash.
- Git: **clawdbotatg** / `clawd@buidlguidl.com`, HTTPS. If push asks for a
  password: `gh auth switch --user clawdbotatg`, push with
  `-c credential.helper='!gh auth git-credential'`, switch back.

## Run

- Harness runs under launchd (`com.clawd.harness`, port 8787, token in
  `.clawd-harness.token`). Check with `launchctl list | grep clawd.harness`.
- Verify JS edits: extract the `<script>`, `node --check` it.
- **Never run `server.py` from this directory for a test** — it resumes the
  real sessions. Copy it to an isolated dir.
- Probes (`tools/*.mjs`) run local headless Chromium with fake sessions —
  never a real session; use real taps on emulated touch, not `.click()`.

## Architecture

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
  `docs/fleet/SUB-ROUTING.md`. Logins die ~30 d after each sign-in (server
  side, rotation doesn't help); the **🔑 sign in to X** button in a session's
  top-right pill (`loginCta`, `SessionManager.login_cta`, `test_login_cta.py`)
  fires only when a signed-out login would be the router's pick for that
  session — never as housekeeping.
- **index.html** — the single UI file, one page, hash routing
  (`#/` projects · `#/p/<pid>` sessions · `…/s/<cid>/tty` terminal ·
  `#/pins` · `#/irons`, `#/i/<id>`; `?breakout=1` in the QUERY = ⏏ breakout
  mode, chrome hidden, one session per window — `docs/DEEPLINKS.md`). Served
  untouched in direct mode; the fleet relay injects `window.__FLEET__`.
  **One copy — edit here, push.**
- **fleet/** — relay + worker driving N harnesses from one phone. Hard
  boundary: fleet code never imports `server.py`; the wire contract is
  `docs/WS-PROTOCOL.md` — keep it in sync with any WS change. Deep docs:
  `fleet/CLAUDE.md`, `docs/fleet/` (ADD-MACHINE.md for new boxes).
  **📱 native iOS app** = `clawd-dictate/ios/Harness` (a WKWebView on the
  relay; exists because a home-screen PWA re-asks for the mic every launch).
  Its passkey works only because the relay serves
  `/.well-known/apple-app-site-association` from `FLEET_AASA_APPS`
  (`fleet/test_aasa.py`) — don't drop that route or env.
- **controller/** — the PM, a WS client like any other; verbs in
  `controller/verbs.py`. **A harness feature doesn't exist to the PM until you
  update three places together: the verb, its MCP description, and the persona**
  (`controller/prompts/private.md`). Deep doc: `docs/CONTROLLER.md`.

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
11. **Self-close is armed-only.** `POST /self/close` (what `bin/harness-close`
    hits) refuses unless the 📑 chip / PM `wrap` verb armed the session within
    `WRAP_TURNS`/`WRAP_TTL_S`. A dirty worktree no longer blocks it (Austin,
    09-11): the close is accepted and the uncommitted files are named in the
    reply for the TLDR. Never widen the arm gate, never force-close from the
    harness side (no call → the arm lapses, the tab stays), and keep the close
    deferred to the Stop so the TLDR reaches the 🗃️ row. The handoff is a
    **local** `HANDOFF-<stamp>.md`, one NEW file per wrap (`handoff_file_name`,
    chosen at the arm and swapped for `{file}` in the prompt — the 📑 chip's
    copy keeps the placeholder): the arm lists `HANDOFF-*.md` in the
    checkout's `.git/info/exclude` and the prompt forbids committing them — a
    handoff in GitHub is noise (Austin, 09-10), and a single overwritten
    `HANDOFF.md` lost every earlier handoff (Austin, 09-15). `test_wrap.py` +
    `tools/wrapprobe.mjs`.

## Periodic

- `python3 bench_naming.py` — re-benchmark the naming model (`BANKR_MODEL`)
  ≈ quarterly; next ≈ 2026-09.
- `python3 tools/mine_quick_prompts.py` — re-mine the composer quick-chips
  ranking ≈ quarterly; next ≈ 2026-11.
