# clawd-harness — orientation for Claude

A web **harness** for driving interactive (subscription-billed) Claude Code
sessions from a browser. Forked from `clawd-console`. `README.md` is the
user-facing overview; this file orients an agent working **on** the code.

## Run / test
- `python3 server.py` → prints a tokenized URL `http://127.0.0.1:8787/?t=<token>`
  (token persisted in `.clawd-harness.token`; or set `CONSOLE_TOKEN`).
- Daemon: `./daemon.sh install [WORKDIR]` (launchd, RunAtLoad + KeepAlive,
  re-`--resume`s sessions). Also `status | logs | restart | uninstall`.
- Smoke test: `python3 smoke_test.py` (reads the token file; asserts both channels).
- **Port 8787**, launchd label **com.clawd.harness**. (clawd-console uses 7878 /
  com.clawd.console — they coexist on purpose.)
- Needs the `claude` CLI on a Claude **subscription** (OAuth, not an API key).
  Pure Python stdlib; xterm.js + a QR lib load from a CDN.
- Verify JS edits: extract the `<script>` from index.html and `node --check` it.
  The app has been verified live in Chrome via the **LAN URL** (see stale-cache note).

## Architecture (one server, multi-session)
- **server.py** — a `SessionManager` owns N `ClaudeSession`s. Each session is one
  interactive `claude` in a PTY (no `-p`), with its own transcript tail + ring
  buffer. `cid` = stable console id (ours; survives claude's id rotation);
  `session_id` = claude's id (rotates on compaction/resume). Registry persisted to
  `.clawd-harness.sessions.json` and `--resume`d on restart.
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
- **AI session naming:** optional. Set `BANKR_API_KEY` + `BANKR_BASE_URL`
  (OpenAI-compatible; or `BANKR_API=anthropic`). Off → first-prompt titles.
  Regenerates at prompt counts `{1, 3, 10}`.
- **index.html** — single page. View switcher (terminal xterm ↔ transcript
  bubbles), **key bar** (sends raw escape seqs to drive TUI menus — works even on
  touch where the terminal is read-only), message box (type/dictate/paste images),
  sessions menu, QR. Mobile defaults to transcript (native scroll, markdown);
  desktop to terminal. Terminal is **read-only on touch** (mobile dictation streams
  self-revising text that xterm forwards as garbled keystrokes).

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
- Roadmap (the reason for the fork): multiple **projects** (workdir groups), the
  **AI controller** layer, Telegram front-end, per-client terminal sizing.
  Multi-session, view switcher, and AI naming already exist.

## Conventions
- **Never commit** runtime/secret files (gitignored): `.clawd-harness.token`,
  `.clawd-harness.session`, `.clawd-harness.sessions.json`,
  `.clawd-harness.hooks*.json`, `.clawd-harness-uploads/`. Scan diffs for leaked
  secrets before committing (a gitleaks pre-commit hook also runs).
- Git identity here (under `~/clawd/`): **clawdbotatg** /
  `clawd@buidlguidl.com`, over **HTTPS**. Remote: `clawdbotatg/clawd-harness`.
- **Browser stale-cache:** a prior app on a port leaves a cached page on
  `127.0.0.1:<port>`. Hard-refresh (Cmd+Shift+R) or use the LAN URL. The server
  sends `Cache-Control: no-store` on the served UI.
