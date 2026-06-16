# clawd-harness WebSocket protocol

The complete wire protocol a client speaks to the harness over its single
WebSocket. A **browser** is one client; the **clawd-fleet proxy worker** is
another — it speaks this exact protocol but forwards frames to the relay instead
of rendering them. **This doc is the contract that lets the fleet drive a
harness without modifying it.** (Source of truth: `server.py` `Handler.handle_ws`
/ `_dispatch`, `ClaudeSession`, `SessionManager`.)

> **Fleet note:** between a mobile and a worker, every frame below is carried
> inside an **end-to-end AES-GCM record** (`fleet-e2e/1`) that the relay cannot
> read — the worker decrypts mobile→harness and encrypts harness→mobile. The
> harness itself is unchanged; the E2E layer wraps this protocol transparently.
> See `docs/fleet/E2E-PROTOCOL.md`.

## Connection

```
GET ws://<host>:8787/ws?t=<TOKEN>      (HTTP/1.1 WebSocket upgrade)
```
- **Port 8787** by default (`PORT`).
- **Token required** — `?t=` must equal the server token (`.clawd-harness.token`
  file, or `CONSOLE_TOKEN` env). A bad/missing token → HTTP 403, no upgrade.
- The page at `/` loads without a token; it just can't open `/ws` without one.

## Two frame types

| WS opcode | Direction | Meaning |
|---|---|---|
| **binary (0x2)** | server→client | raw PTY bytes for the session this client is subscribed to (feed straight into a terminal emulator / xterm.js) |
| **text (0x1)** | both ways | JSON control + structured events (everything below) |

The harness never parses the terminal's visual output — binary frames are the
literal ANSI byte stream. Structured meaning comes from the transcript + hook
events, not from scraping the terminal.

## Subscription model (important)

- A client is subscribed to **at most one session at a time**, tracked server-side
  as `client.cid`. `{type:"subscribe", cid}` switches it.
- **Per-subscription** streams (only the subscribed client gets these): binary PTY
  bytes, `hello`, `transcript`.
- **Broadcast** streams (every connected client gets these): `projects`,
  `sessions`, `hook`, `exit`, `reload`, `restart`.

➡ For the fleet, this means: to let two phones watch two different sessions on the
same machine, the proxy worker opens **one harness WS connection per remote
viewer** (each with its own `client.cid`).

---

## Client → server (control frames)

| `type` | Fields | Effect |
|---|---|---|
| `subscribe` | `cid` | Attach to that session's live stream. Server immediately sends a ring-buffer byte snapshot, a `hello`, then replays recent `transcript` history. |
| `list` | — | Server replies with `projects` then `sessions` snapshots. |
| `new` | `pid` | Create a session in project `pid`. Server replies `{type:"focus", cid}` with the new id, and broadcasts `sessions`. |
| `close` | `cid` | Kill that session (SIGTERM) and detach viewers. Files on disk untouched. |
| `createProject` | `name` | Create a new public GitHub repo under `GH_OWNER` and adopt it (async; status broadcasts via `projects`). |
| `addProject` | `repoUrl` | Clone a repo and adopt it (async). Input normalized: full URL as-is; `owner/repo` and bare `repo` resolved against github.com. |
| `input` | `data`, `cid?` | Raw keystrokes → PTY. `data` is a UTF-8 string (incl. escape seqs for TUI menus). Falls back to `client.cid` if `cid` omitted. |
| `send` | `text`, `cid?` | High-level: type `text`, wait for the paste to settle, then submit `\r`. Use this to "send a message/prompt". |
| `resize` | `cols`, `rows`, `cid?` | Resize the PTY window. |
| `restart` | `reason?` | Request a graceful self-restart (fires once all sessions idle). |
| `restartCancel` | — | Cancel a pending restart. |

`input` vs `send`: `send` is what you want for prompts — it handles the
TUI's paste-vs-submit timing (`SEND_SETTLE`). `input` is for raw control
(arrow keys, escape sequences to drive `claude`'s menus).

**No `removeProject`:** disk is the source of truth for the project list. The
server reconciles the in-memory set against the repos in `projects/` every ~1s
(and on boot) — a new repo dir is adopted, a vanished one is dropped (its now
cwd-less sessions killed), and the change is broadcast via `projects`. To remove
a project, delete its folder on disk; there is no wire message for it.

---

## Server → client (frames)

### On connect (every client, immediately)
```jsonc
{ "type":"projects", "projects":[<projectMeta>...], "boot":"<BOOT_ID>" }
{ "type":"sessions", "sessions":[<sessionMeta>...], "current":"<cid|null>" }
// + a restart-state frame if a restart is already pending
```
`boot` is a per-process id; the browser auto-reloads when it changes after a
reconnect (i.e. the server restarted).

### On `subscribe` (that client only)
```jsonc
// 1) a binary frame: recent PTY bytes (ring buffer snapshot)
// 2) then:
{ "type":"hello", "cid", "pid", "sessionId", "title", "workdir",
  "busy":bool, "waiting":bool, "tool":<string|null>, "cols":int, "rows":int }
// 3) then recent transcript history, each:
{ "type":"transcript", "cid", "event":<event>, "history":true }
```

### Live, ongoing
```jsonc
{ "type":"transcript", "cid", "event":<event> }      // new transcript line (history absent/false)
{ "type":"hook", "cid", "event":<hookName>, "busy":bool, "waiting":bool, "tool":<str|null>, "data":{...} }
{ "type":"focus", "cid" }                              // reply to a "new" you sent
{ "type":"exit", "cid" }                               // the claude process for cid exited
{ "type":"projects", "projects":[...], "boot" }        // re-broadcast on any project change
{ "type":"sessions", "sessions":[...], "current" }     // re-broadcast on any session change
{ "type":"reload" }                                    // index.html changed on disk → browser should reload
{ "type":"restart", "pending":bool, "reason", "busy":int }   // restart state (banner)
{ "type":"restart", "state":"go" }                     // restart firing now (process about to exit)
```

---

## Object shapes

### projectMeta
```jsonc
{ "pid", "name", "path", "repoUrl", "status":"ready|cloning|error",
  "error", "sessionCount":int, "busyCount":int, "waitingCount":int,
  "created":float, "pinned":bool }
```

### sessionMeta
```jsonc
{ "cid", "pid", "title", "desc", "named":bool, "busy":bool, "waiting":bool,
  "tool":<str|null>, "status":"blocked|working|idle", "digest":str,
  "blocked_on":str, "sessionId", "promptCount":int,
  "lastActive":float, "created":float, "alive":bool }
```
- `waiting` = the session is blocked on an interactive TUI prompt (a permission
  request, `AskUserQuestion`, or `ExitPlanMode`) and needs a human answer — it
  looks idle from the outside but isn't. `busy` is still `true` while `waiting`
  (the turn is in flight, just parked). In the **projectMeta** counts a waiting
  session is tallied in `waitingCount` *instead of* `busyCount` (mutually
  exclusive) so a blocked session reads as "needs you", not "working".
- `status` = the deterministic, LLM-free roll-up of `busy`/`waiting` for a
  controller's attention queue: `blocked` (needs a human now) > `working` (turn
  in flight) > `idle`.
- `digest` = a volatile one-line "what this session is doing right now" (LLM,
  refreshed on every `Stop` — see naming below). `""` until the first turn ends
  or if naming is unconfigured. The *stable* label is `title`/`desc`; the digest
  is the live state. Held in memory only (not persisted; regenerated each turn).
- `blocked_on` = the open question if the turn ended by asking the human
  something in plain text (LLM-inferred) — a *soft* block the `waiting` flag
  (TUI prompts only) misses. `""` when not blocked. These three feed the AI
  controller's read-model; see `docs/CONTROLLER.md`.
- `cid` = stable console id (ours; survives claude's id rotation). **Address
  sessions by `cid`, never `sessionId`.**
- `sessionId` = claude's own id; rotates on compaction/resume.

### transcript `event` (from `_slim_event`)
One of:
```jsonc
{ "role":"user", "text" }
{ "role":"command", "text" }                  // a slash-command invocation
{ "role":"system", "text" }                   // local-command stdout
{ "role":"tool_result", "results":[...] }
{ "role":"assistant", "text"?, "tools"? }     // tools = list of tool_use summaries
{ "role":"result", "subtype", "is_error", "duration_ms", "usage" }
```

### hook `event` names + their `data`
The turn-lifecycle signal that drives the working/idle/blocked pill. `busy` and
`waiting` are the session's current state after this hook.

| `event` | `busy` | `waiting` | `data` |
|---|---|---|---|
| `UserPromptSubmit` | true | false | `{ "prompt" }` |
| `PreToolUse` | true | true *iff* tool ∈ {`AskUserQuestion`,`ExitPlanMode`}, else false | `{ "tool" }` |
| `PostToolUse` | true | false | `{ "tool", "duration_ms" }` |
| `Stop` | **false** | false | `{ "last" }` ← the last assistant message (the turn's answer) |
| `Notification` | — | true *iff* `busy` (a permission/input block), else unchanged | `{ "message" }` |
| `SessionStart` | false | false | `{ "source", "model" }` |
| `SessionEnd` | — | false | `{ "reason" }` |

➡ **To detect "the session is blocked waiting for a human": watch `waiting`.**
Every non-`Notification` hook clears it (the prompt got answered → progress
resumed); `Notification` (mid-turn) and `PreToolUse` of the two interactive
tools set it.

➡ **To detect "the turn finished and here's the answer": watch for a `hook`
frame with `event:"Stop"` — `data.last` is the assistant's final message.**

---

## Minimal "send a prompt, get the answer" flow (what a thin fleet client needs)

```text
→ {type:"list"}                          (discover projects/sessions)
→ {type:"new", pid:"<pid>"}              (or pick an existing cid)
← {type:"focus", cid:"<cid>"}
→ {type:"subscribe", cid:"<cid>"}
← hello + history
→ {type:"send", cid:"<cid>", text:"<the prompt>"}
← hook UserPromptSubmit (busy=true) … PreToolUse/PostToolUse … 
← hook Stop (busy=false) with data.last = the answer
   (and live transcript + binary PTY frames throughout)
```

For a full remote terminal/transcript UI, also relay the binary frames and
`transcript` events verbatim — they already carry everything the harness's own
`index.html` renders.
