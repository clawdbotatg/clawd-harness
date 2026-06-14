# clawd-harness — orientation for Claude

A web **harness** for driving interactive (subscription-billed) Claude Code
sessions from a browser. Forked from `clawd-console`. `README.md` is the
user-facing overview; this file orients an agent working **on** the code.

> **Multi-machine?** See the sibling repo **`clawd-fleet`** (`../clawd-fleet`,
> `github.com/clawdbotatg/clawd-fleet`). It's an abstraction layer that drives N
> harnesses (one per machine) from one phone via a public relay. It treats this
> harness as a black box reached over the WebSocket — **don't add fleet code
> here.** The wire contract it depends on is **[`docs/WS-PROTOCOL.md`](docs/WS-PROTOCOL.md)**;
> keep that doc in sync if you change the WS protocol in `server.py`.

## Run / test
- `python3 server.py` → prints a tokenized URL `http://127.0.0.1:8787/?t=<token>`
  (token persisted in `.clawd-harness.token`; or set `CONSOLE_TOKEN`).
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
  Pure Python stdlib; xterm.js + a QR lib load from a CDN.
- Verify JS edits: extract the `<script>` from index.html and `node --check` it.
  The app has been verified live in Chrome via the **LAN URL** (see stale-cache note).

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
  Off → first-prompt titles. Regenerates at prompt 1, then every 5 (5, 10, 15,
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
  (type/dictate/paste images), QR. The app opens on the projects rung; mobile
  defaults to transcript for a session (native scroll, markdown), desktop to
  terminal. Terminal is **read-only on touch** (mobile dictation streams
  self-revising text that xterm forwards as garbled keystrokes).
- **URL routing** — nav state lives in the **hash** (the `?t=` token stays in the
  query): `#/` projects · `#/p/<pid>` sessions · `#/p/<pid>/s/<cid>` transcript ·
  `…/tty` terminal. So a reload (or a shared link) lands back on the same
  project/session/depth, and back/forward work. `setView`/`subscribe` write it via
  `syncUrl()`; on boot `parseHash()` seeds `pendingNav`, which `resolvePendingNav()`
  applies once the server's project then session snapshots arrive (gracefully
  falling back if the named project/session is gone). `syncUrl()` no-ops while a
  restore is pending; a `lastWrittenHash` guard keeps our own writes from
  re-triggering the `hashchange` handler. Creating a session switches you into it
  (`pendingNewFocus` → the server's `focus` reply opens the new `cid`).

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
  front-end, per-client terminal sizing. Multi-session, multi-project (the
  projects layer), view switcher, and AI naming already exist.

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
