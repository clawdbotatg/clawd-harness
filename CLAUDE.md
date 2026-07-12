# clawd-harness — orientation for Claude

A web **harness** for driving interactive (subscription-billed) Claude Code
sessions from a browser. Forked from `clawd-console`. `README.md` is the
user-facing overview; this file orients an agent working **on** the code.

> **Relationship to `claude-p-agent`** (`projects/claude-p-agent`): that repo is the
> generic agent pattern — `claude -p` + a `CLAUDE.md` persona + trust-tagged channels,
> with a build tool that delegates real coding to managed Claude Code sessions. **This
> harness is the worker/session manager that build tool delegates to:** when the agent
> runs its `code` helper, it drives this harness (over the WS protocol) to spawn and
> supervise the real `claude` worker sessions. claude-p-agent is the brain pattern;
> the harness is the engine behind its hands. (The live voice agent's adapter is
> `projects/clawd-video-chat`.)

> **Multi-machine?** The fleet (relay/worker layer that drives N harnesses from
> one phone via a public relay) now lives **in this repo** under **[`fleet/`](fleet/)**
> — see **[`fleet/CLAUDE.md`](fleet/CLAUDE.md)**. It was folded in from the former
> standalone `clawd-fleet` repo (now archived). The **boundary is preserved as a
> code-discipline rule**: `fleet/` code must never import `server.py` or reach into
> harness internals — the worker is just another WS client. The wire contract it
> depends on is **[`docs/WS-PROTOCOL.md`](docs/WS-PROTOCOL.md)**; keep that doc in
> sync if you change the WS protocol in `server.py`. Fleet deep docs:
> **[`docs/fleet/`](docs/fleet/)**. **Adding a machine to the fleet?** Hand the new
> box **[`docs/fleet/ADD-MACHINE.md`](docs/fleet/ADD-MACHINE.md)** — a self-contained
> checklist (harness + worker + the E2E `cryptography` dep and shared passkey file).
>
> **`index.html` is the single, *unified* UI** shared by both modes — mode-aware via
> `window.__FLEET__`: the harness serves it untouched (direct mode); the fleet relay
> (`fleet/relay.py` `_serve_file`) injects the flag for fleet mode (machines rung +
> passkey). There is now **one copy** (the old cp-to-fleet sync ritual is gone). To
> change the fleet UI: edit this `index.html`, then `git push` + `git pull` on the
> box — the box is now a **git checkout** at `~/clawd-harness`, not an scp dir (see
> `docs/fleet/DEPLOY.md`). The fleet/passkey code here is dormant unless `__FLEET__`
> is set.

## Run / test
- `python3 server.py` → prints a tokenized URL `http://127.0.0.1:8787/?t=<token>`
  (token persisted in `.clawd-harness.token`; or set `CONSOLE_TOKEN`).
- **Deploy to the fleet = `git push` to main.** Every harness runs
  `auto_update_loop` (~5 min + jitter): on-main + clean-worktree + ff-only →
  it pulls itself, then the file watcher hot-reloads browsers (index.html) or
  gracefully restarts (server.py). A dirty worktree (live-edit in progress) is
  skipped. Opt a box out with `AUTO_PULL=0`. No ssh, no manual pulls — **for
  harness machines**. The one exception is the **relay box** (serves the fleet
  UI at `h.atg.link`): it runs no harness, so nothing auto-pulls it — a UI
  change is NOT live on the fleet URL until the box's `~/clawd-harness`
  checkout is pulled (no restart needed; the relay reads `index.html` per
  request). See `docs/fleet/DEPLOY.md` for the box details.
- Daemon: `./daemon.sh install [WORKDIR]` (launchd, RunAtLoad + KeepAlive,
  re-`--resume`s sessions). Also `status | logs | restart | uninstall`.
- Smoke test: `python3 smoke_test.py` (reads the token file; asserts both channels).
- **Port 8787**, launchd label **com.clawd.harness**. (clawd-console uses 7878 /
  com.clawd.console — they coexist on purpose.)
- **Is it running?** It usually already is (launchd `KeepAlive`). Don't check
  with `pgrep -f server.py` — launchd's invocation doesn't arg-match, so that
  returns nothing even while it's up. Use `launchctl list | grep clawd.harness`
  (shows the PID) or `lsof -nP -iTCP:8787 -sTCP:LISTEN`. So: edit `index.html` →
  it live-reloads (see below); no need to start a server first.
- Needs the `claude` CLI on a Claude **subscription** (OAuth, not an API key).
  Pure Python stdlib; xterm.js loads from a CDN.
- Verify JS edits: extract the `<script>` from index.html and `node --check` it.
  The app has been verified live in Chrome via the **LAN URL** (see stale-cache note).
- **Watch the UI run yourself — `tools/uiprobe.mjs`.** For any *visual/DOM* bug
  (textarea sizing, layout, a button that won't repaint) don't reason blind: drive
  the running app from a **local headless Chromium** and read the real DOM.
  `cd tools && npm i` once (playwright browsers are already cached on this machine,
  so it's just `playwright-core`), then
  `node uiprobe.mjs` (snapshot + screenshot the projects rung) or
  `node uiprobe.mjs --hash '#/p/self/s/<cid>/tty' --box` (deep-link a session and
  assert the composer grows on fill + shrinks on clear — prints `{resting,tall,cleared}`,
  exit-codes for a verify flow; it clears the box rather than sending, so no real
  message hits a live session). **Why this and not the `claude-in-chrome` MCP
  browser:** that browser is *remote* (a cloud Chrome on another network) and
  **cannot reach `127.0.0.1:8787`** — every navigate fails `ERR_CONNECTION_REFUSED`.
  A process launched from the Bash tool is on *this* machine (same as `server.py`),
  so it can. This is the loop that turns "guess a fix, commit, ask the human to
  eyeball it" into "run it, read the number."

## Architecture (one server, multi-project, multi-session)
- **server.py** — a `SessionManager` owns N `Project`s and N `ClaudeSession`s.
  A **project** is a git repo under `projects/` (`PROJECTS_DIR`, gitignored); a
  session's `claude` runs with `cwd` = its project's path (`ClaudeSession.pid` →
  `Project`). Each session is one interactive `claude` in a PTY (no `-p`), with
  its own transcript tail + ring buffer. `cid` = stable console id (ours;
  survives claude's id rotation); `session_id` = claude's id (rotates on
  compaction/resume). Registry persisted to `.clawd-harness.sessions.json` as
  `{"projects":[…],"sessions":[…]}` and `--resume`d on restart. **Disk is the
  source of truth for the project list:** `reconcile_projects()` runs on the
  ~1s `watch_ui` loop (and on boot) — it adopts any new git repo under
  `projects/` and drops any whose folder has vanished (killing its now
  cwd-less sessions), broadcasting the change. The registry persists projects
  only as a pid↔path memo so ids stay stable across reboots.
- **Projects layer:** create a new **public** repo under `GH_OWNER`
  (`clawdbotatg`) via `gh repo create … --clone`, or clone a repo — both run
  async in a thread with a `cloning → ready|error` status broadcast. Clone input
  is normalized: a full git URL/path is used as-is, while `owner/repo` and a bare
  `repo` name are resolved against `github.com` (bare → `GH_OWNER`), so typing
  `slop-computer-live` clones `github.com/clawdbotatg/slop-computer-live`.
  **Creation needs `gh` authenticated in the server's environment** (cloning a
  public URL does not). **There is no in-app "remove":** to drop a project you
  delete its repo folder under `projects/` yourself and the reconcile loop
  follows within ~1s (the pinned self-project lives outside `projects/`, so it's
  never touched).
- **Accounts (subscription routing):** the harness can hold N Claude
  subscription logins at once — each account = a config dir under
  `~/.clawd-accounts/<name>` (`CLAUDE_CONFIG_DIR` isolates the credential
  store; `default` = plain `~/.claude`). Sign-in happens **in the UI**: the
  accounts panel at the foot of the projects rung spawns a claude session
  under the fresh dir and you complete OAuth in its terminal. A poller tracks
  per-account usage via Claude's (undocumented — always degrade) OAuth usage
  endpoint; new sessions spawn under the ACTIVE account, auto-switched to the
  non-exhausted pool whose WEEKLY window resets soonest (use-it-or-lose-it;
  headroom is only the tie-break — see `_route_key`), debounced via `SUB_*`
  env knobs. Sessions
  record `account`+`config_dir` at spawn so `--resume` finds the right dir —
  but they don't stay pinned: the sweep hands an idle session off to a
  better pool, both as a drain rescue and as a **rebalance** onto the
  reset-soonest pool (`_rebalance_win`, `SUB_REBALANCE*` knobs).
  Deep doc: [`docs/fleet/SUB-ROUTING.md`](docs/fleet/SUB-ROUTING.md); what the
  accounts panel should display + mis-bound-login runbook:
  [`docs/fleet/ACCOUNTS-PANEL.md`](docs/fleet/ACCOUNTS-PANEL.md) (key trap: one
  email can hold seats in several orgs — the ORG is the usage pool); probe:
  `python3 tools/usage_probe.py [config_dir]`.
- **Self-project:** the harness always injects *itself* as a **pinned** project
  (`SELF_PID="self"`, `path=HERE`, top of the list, never persisted —
  re-injected each boot) so you can open a session and **live-edit the running
  app**. It's the one project whose path is outside `PROJECTS_DIR`.
- **Graceful self-restart** (companion to live-editing): `watch_ui` polls
  `RESTART_FILES` (`server.py`, `.clawd-harness.env` — both read only at boot);
  a change calls `MGR.request_restart(reason)`, which flags `restart_pending`,
  surfaces a banner in every browser, and **waits until no session is `busy`**
  before `_execute_restart` SIGTERMs the claude children and `os._exit(0)`s —
  launchd (`KeepAlive=true`) respawns us and sessions `--resume`. So an edit to
  the harness never kills an in-flight turn. The browser auto-reloads on the
  `BOOT_ID` change after reconnect. Manual: WS `{type:"restart"}` /
  `{type:"restartCancel"}`.
- **Live-reload of the UI (no manual reload needed):** `watch_ui` *also* polls
  `WATCH_FILES` (`index.html`) and, on an mtime change, broadcasts WS
  `{type:"reload"}` → every open browser calls `location.reload()`
  (`index.html` ~L495). So **saving `index.html` is enough — all open tabs
  hard-reload themselves within ~1s**; never tell the user to reload manually,
  and don't restart the server for a UI-only edit (that's only for
  `RESTART_FILES`). Caveat: this needs `server.py` to be running.
- **One WebSocket per browser, multiplexed** — a client subscribes to one session
  (its PTY bytes + transcript); session metadata (titles, busy badges) fans out to
  all clients.
- **Channels:** WRITE = keystrokes → PTY; READ (visual) = raw PTY bytes → xterm.js
  renders the ANSI; READ (structured) = transcript JSONL tailed → slim events. We
  never parse the terminal's "weird text."
- **Hooks → turn signal:** injected via `claude --settings <generated>` →
  each hook `curl`s stdin to `POST /hook` → broadcasts `hook` events
  (Stop / UserPromptSubmit / Pre+PostToolUse / SessionStart+End). Drives the
  working/idle pill. **Stop** carries `last_assistant_message`.
- **Images:** `POST /upload` saves to `.clawd-harness-uploads/`; the path is folded
  into the message and claude `Read`s it (vision works by file path).
- **AI session naming:** optional. Set `BANKR_API_KEY` + `BANKR_BASE_URL` +
  `BANKR_API` (`openai` | `anthropic` | `bankr`). `bankr` = OpenAI-compatible body
  at `https://llm.bankr.bot/v1/chat/completions` authed with an `X-API-Key` header.
  Off → first-prompt titles. Regenerates at prompt 1, then every 3 (3, 6, 9,
  …) via `name_at_prompt()` — naming is cheap + async, so a steady cadence keeps
  a long session's title sharp. Secrets
  load from a gitignored **`.clawd-harness.env`** (`_load_env_file` at boot — the
  launchd daemon doesn't inherit your shell env, so this is the way).
- **Right model for the right job:** naming is a cheap, frequent, fire-and-forget
  labeler (~900 input tokens, 3×/session, async) — so `BANKR_MODEL` = **`qwen3-coder`**,
  the winner of a full 41-model cost+speed+reliability survey: ~$0.032 per 1,000
  calls (½ the cost of `gemini-3.1-flash-lite`), ~510ms (fastest reliable), 5/5
  clean JSON, and on-domain (a code model naming code sessions). `deepseek-v3.2`
  is an equal runner-up. Three traps the survey exposed: **reasoning models**
  (`gemini-3-flash`, the `-pro`/`gpt-5.4`/`glm`/`kimi` tiers) blow the 120-token
  budget *thinking* and return `null` content; **fast ≠ cheap** (`grok-4.20` was
  quickest but 18× the price); **cheapest ≠ usable** (`gemma-4-*` only emit clean
  JSON 1/5, wrapping it in prose). The future **AI controller** layer is a
  *different* job (reasoning, tool decisions) and should pick its own stronger
  model — likely one of the very reasoning models that are wrong for naming.
- **Re-benchmark the naming model regularly** — new models ship constantly. Run
  **`python3 bench_naming.py`** (no args → pulls the full live model list and
  tests every model on the real naming prompt for JSON-reliability + median
  latency, ranks them, recommends one). If a model clearly beats the incumbent,
  update `BANKR_MODEL` in `.clawd-harness.env`. **Cadence: roughly quarterly —
  last run 2026-06, next ≈2026-09.** The script reuses `server.NAME_SYS_PROMPT`
  and the `.clawd-harness.env` creds, so it never drifts from the app or hardcodes
  a key.
- **index.html** — single page. A 4-level swipe stack — **projects → sessions →
  transcript → tty** (`LEVELS`); swipe right climbs out, left dives in. Projects
  page = card list + an add row (name → create repo, git URL → clone). Sessions
  page is scoped to the selected `currentPid`. View switcher (terminal xterm ↔
  transcript bubbles), **key bar** (sends raw escape seqs to drive TUI menus —
  works even on touch where the terminal is read-only), message box
  (type/dictate/paste images). The app opens on the projects rung; a session
  opens as the live terminal on every device (the transcript view was pulled —
  `DEEP_VIEW`). Terminal is **read-only on touch by default** (mobile dictation
  streams self-revising text that xterm forwards as garbled keystrokes) — the
  key bar's **⌨ toggle** opts into direct typing (soft keyboard → PTY) for TUI
  prompts/shells; it resets to read-only on leaving the tty view, and dictation
  should still go through the composer. **Shared-PTY
  sizing:** one PTY can't render two geometries, so resize frames are size
  *claims* and the server follows a single owner — deliberate acts (opening the
  tty view, resizing the window, typing/sending) claim it; reconnect/refit
  maintenance resizes only apply if you already own it; leaving the view or
  going hidden releases it (fallback: most recent remaining viewer). See
  `claim_resize` in server.py + the `resize`/`ttySize` rows in
  `docs/WS-PROTOCOL.md`. Don't regress this to last-resize-wins: it's what
  stopped a background desktop tab from yanking the terminal down to/up from
  phone size mid-use. Attaching to a PTY another device sized (`hello` dims ≠
  ours) renders that device's replay mangled — once our claim is applied the
  client auto re-subscribes for a clean replay (`staleGeomReplay` in
  index.html), so no manual reload. The server also drops the ring buffer
  whenever a claim changes the PTY *width* (`_apply_size`): bytes painted for
  another width rewrap into garbage in any replay (the mobile
  scroll-up-shredded-scrollback bug), so replays only reach back to the last
  width change — older history lives in the transcript view.
- **URL routing** — nav state lives in the **hash** (the `?t=` token stays in the
  query): `#/` projects · `#/p/<pid>` sessions · `#/p/<pid>/s/<cid>` transcript ·
  `…/tty` terminal. So a reload (or a shared link) lands back on the same
  project/session/depth, and back/forward work. `setView`/`subscribe` write it via
  `syncUrl()`; on boot `parseHash()` seeds `pendingNav`, which `resolvePendingNav()`
  applies once the server's project then session snapshots arrive (gracefully
  falling back if the named project/session is gone). `syncUrl()` no-ops while a
  restore is pending; a `lastWrittenHash` guard keeps our own writes from
  re-triggering the `hashchange` handler. Creating a session switches you into it
  (`pendingNewFocus` → the server's `focus` reply opens the new `cid`). **Building
  a link into a specific session (incl. the fleet `#/m/<machine>/p/<projectKey>/s/<cid>`
  form + how notifications construct them): see [`docs/DEEPLINKS.md`](docs/DEEPLINKS.md).**

## Two non-obvious gotchas (baked into server.py — don't regress)
1. **`SCRUB_ENV`** — scrub `CLAUDECODE` / `CLAUDE_CODE_*` / `ANTHROPIC_API_KEY`
   etc. from the child env, or a nested `claude` runs in embedded mode (no
   transcript written) and bills metered API instead of the subscription.
2. **`SEND_SETTLE`** — pause between typing text and the `\r`, or claude's TUI
   treats `text`+`\r` as a paste and the `\r` doesn't submit. Short messages use
   `SEND_SETTLE_MIN` (~0.7s); big/multi-line use `SEND_SETTLE` (~1.5s).

## Known issues / next
- **Transcript tailer logs `tailing …` repeatedly** (busy-reattach loop, inherited
  from console) — worth fixing.
- Roadmap (the reason for the fork): the **AI controller** layer, Telegram
  front-end. Multi-session, multi-project (the projects layer), view switcher,
  and AI naming already exist. ("Per-client terminal sizing" was resolved as
  the size-ownership policy above — true per-viewer rendering would need a
  server-side terminal emulator per client and isn't planned.)

## Conventions
- **Never commit** runtime/secret files (gitignored): `.clawd-harness.token`,
  `.clawd-harness.session`, `.clawd-harness.sessions.json`,
  `.clawd-harness.hooks*.json`, `.clawd-harness-uploads/`, `projects/` (the
  cloned repos). Scan diffs for leaked secrets before committing (a gitleaks
  pre-commit hook also runs).
- Git identity here (under `~/clawd/`): **clawdbotatg** /
  `clawd@buidlguidl.com`, over **HTTPS**. Remote: `clawdbotatg/clawd-harness`.
- **Browser stale-cache:** a prior app on a port leaves a cached page on
  `127.0.0.1:<port>`. Hard-refresh (Cmd+Shift+R) or use the LAN URL. The server
  sends `Cache-Control: no-store` on the served UI.
