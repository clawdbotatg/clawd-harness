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
| `subscribe` | `cid` | Attach to that session's live stream. Server immediately sends a `hello`, then a ring-buffer byte snapshot, then replays recent `transcript` history (see "On `subscribe`" below, incl. the unknown-cid error reply). |
| `list` | — | Server replies with `projects` then `sessions` snapshots. |
| `new` | `pid`, `account?` | Create a session in project `pid`, spawned under the ACTIVE subscription account (or the named `account` override). Server replies `{type:"focus", cid}` with the new id, and broadcasts `sessions`. |
| `accountAdd` | `name` | Register a new subscription account (config dir under `~/.clawd-accounts/<name>` + settings symlinks) and spawn its **sign-in session** — a normal claude in the self project under that `CLAUDE_CONFIG_DIR`, where the user completes the OAuth login. Replies `{type:"focus", cid}` for the sign-in session; broadcasts `accounts`. Re-invoking on a still-pending account opens another sign-in session; a no-op on a ready one. |
| `accountUse` | `name` | Flip which account NEW sessions spawn under (manual switch; running sessions untouched). Refused for a pending account. Broadcasts `accounts`. |
| `accountRemove` | `name` | Drop an account from the routing roster. Logs nothing out (config dir + Keychain credential stay; running sessions keep their recorded dir). Refused when it would leave no ready account; removing the ACTIVE account re-routes new spawns to the most headroom. `default` stays removed (it's only re-injected on an empty roster; `accountAdd` with name `default` re-adopts it, sans ceremony). Broadcasts `accounts`. |
| `accountsRefresh` | — | Poll every account's usage now (instead of waiting out the TTL). Broadcasts `accounts` on change. |
| `close` | `cid` | Kill that session (SIGTERM) and detach viewers. Files on disk untouched. |
| `pin` | `cid`, `on?` | 📌 park a session on the pin board (`on:true`, the default) or restore it to the tabs (`on:false`). Pure metadata — the claude process is untouched and can still be prompted via `send`; the UI keeps pinned sessions off the tab strip/sessions rung and shows them on the board instead. Persisted (survives restarts); broadcasts `sessions`. |
| `createProject` | `name` | Create a new public GitHub repo under `GH_OWNER` and adopt it (async; status broadcasts via `projects`). |
| `addProject` | `repoUrl` | Clone a repo and adopt it (async). Input normalized: full URL as-is; `owner/repo` and bare `repo` resolved against github.com. |
| `addLocalProject` | `path` | Register an EXISTING folder anywhere on the machine's disk (absolute or `~` path) as a **private local project** (`kind:"local"`): sessions run inside it like any project, but the harness never runs gh/git-remote operations on it and never stores/broadcasts a repo URL. Synchronous — the project appears `ready` immediately. Rejected (with an `error` frame to the sender) for: non-directories, `/` or `~` itself, paths under `projects/` (auto-managed) or the harness's own dir (the pinned self-project). Re-adding the same resolved path is a no-op. |
| `removeProject` | `pid` | Detach a `kind:"local"` project: drop its registry entry and close its sessions. **Never touches the folder on disk.** Silently ignored for gh projects (they keep the delete-the-folder contract below) and the pinned self-project. |
| `input` | `data`, `cid?` | Raw keystrokes → PTY. `data` is a UTF-8 string (incl. escape seqs for TUI menus). Falls back to `client.cid` if `cid` omitted. |
| `send` | `text`, `cid?` | High-level: type `text`, wait for the paste to settle, then submit `\r`. Use this to "send a message/prompt". |
| `resize` | `cols`, `rows`, `cid?`, `claim?` | A size CLAIM, not a command — one PTY serves many differently-sized viewers, so the server sizes it to a single OWNER. `claim:true` (a deliberate act on that device: opening the tty view, resizing the window) takes ownership; without it the size only applies if the sender already owns the PTY (or nobody does). `input`/`send` from a sized viewer also takes ownership (you drive it, it fits you). `cols`/`rows` of `0` releases the claim (viewer left the tty view / went hidden) — ownership falls back to the most recently sized remaining viewer. Applied changes broadcast `ttySize`. Pre-policy servers treat every frame as an unconditional resize, so old/new mixes degrade to the last-write-wins behavior. |
| `restart` | `reason?` | Request a graceful self-restart (fires once all sessions idle). |
| `restartCancel` | — | Cancel a pending restart. |
| `ping` | `id?` | Liveness probe. Server immediately replies `{type:"pong", id}` (echoing `id`). Lets a client prove the *full* path is live (in fleet: browser→relay→worker→harness and back over the e2e channel) before deciding whether to repaint in place vs. tear down + re-subscribe. A pre-`pong` harness just ignores it — the prober falls back to reconnect on timeout, so it's backward-safe. |

`input` vs `send`: `send` is what you want for prompts — it handles the
TUI's paste-vs-submit timing (`SEND_SETTLE`). `input` is for raw control
(arrow keys, escape sequences to drive `claude`'s menus).

**Removal is kind-dependent.** For gh projects, disk is the source of truth:
the server reconciles the in-memory set against the repos in `projects/` every
~1s (and on boot) — a new repo dir is adopted, a vanished one is dropped (its
now cwd-less sessions killed), and the change is broadcast via `projects`. To
remove a gh project, delete its folder on disk; `removeProject` is ignored for
them. `kind:"local"` projects are the inverse: they exist only in the registry
(their paths live outside `projects/`, invisible to the disk reconcile), are
removed only via `removeProject`, and are **never auto-dropped** — a missing
local path flips the project to `status:"error"` (`"folder missing"`) after
~30s of continuous absence (`LOCAL_GONE`) and heals back to `ready` when the
path returns (network-volume blips survive; sessions keep running throughout).

**Mixed versions:** `kind` is absent from old servers — consumers treat absent
as `"gh"`. An old UI/worker keys a local project as `name:<name>` instead of
`local:<machine>:<path>` (see DEEPLINKS.md) — cards render, deep links may not
resolve; degrades, doesn't break.

---

## Server → client (frames)

### On connect (every client, immediately)
```jsonc
{ "type":"projects", "projects":[<projectMeta>...], "boot":"<BOOT_ID>" }
{ "type":"sessions", "sessions":[<sessionMeta>...], "current":"<cid|null>" }
{ "type":"accounts", "accounts":[<accountMeta>...], "active":"<name>", "auto":bool }
// + a restart-state frame if a restart is already pending
```
`boot` is a per-process id; the browser auto-reloads when it changes after a
reconnect (i.e. the server restarted).

### On `subscribe` (that client only)
```jsonc
// 1) FIRST the hello — it names the cid the bytes that follow belong to, so a
//    client can gate painting on it (binary PTY frames carry no session id):
{ "type":"hello", "cid", "pid", "account", "sessionId", "title", "workdir",
  "busy":bool, "waiting":bool, "tool":<string|null>, "cols":int, "rows":int }
//    cols/rows = the PTY's CURRENT size (some viewer's claim, or the boot default)
// 2) then binary frames: recent PTY bytes. The ring buffer only reaches back
//    to the last WIDTH change: bytes painted for another width can't render
//    right anywhere, so the server drops them when a size claim changes cols
//    (rows-only changes keep the ring). When the ring snapshot is SHALLOW
//    (< SEED_RING_MAX, i.e. just fenced or fresh boot), it is preceded by a
//    "seed" byte frame: the recent conversation rendered from the transcript
//    as soft-wrapped plain text + a screenful of newlines, so the terminal
//    still has scrollback to show above the live screen (this is what makes
//    scroll-up on a phone show the session's past instead of nothing).
// 3) then recent transcript history, each:
{ "type":"transcript", "cid", "event":<event>, "history":true }
```
An **unknown `cid`** detaches the client from its previous session and replies
`{ "type":"error", "cid", "error":"no such session: …" }` — never a silent
no-op that would leave the previous session's stream flowing (that's how
"another session's output paints into this terminal" used to happen).

### Live, ongoing
```jsonc
{ "type":"transcript", "cid", "event":<event> }      // new transcript line (history absent/false)
{ "type":"hook", "cid", "event":<hookName>, "busy":bool, "waiting":bool, "tool":<str|null>, "data":{...} }
{ "type":"focus", "cid" }                              // reply to a "new" you sent
{ "type":"exit", "cid" }                               // the claude process for cid exited
{ "type":"ttySize", "cid", "cols":int, "rows":int }    // to that session's subscribers: the PTY was re-sized to a viewer's claim (see `resize`)
{ "type":"projects", "projects":[...], "boot" }        // re-broadcast on any project change
{ "type":"sessions", "sessions":[...], "current" }     // re-broadcast on any session change
{ "type":"accounts", "accounts":[...], "active", "auto" }  // subscription logins/usage/active changed
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
  "created":float, "pinned":bool, "kind":"gh|local", "lastTouched":float,
  "emoji":str }
// kind absent (old server) ⇒ "gh". kind:"local" always has repoUrl:"" —
// enforced in the Project constructor, a local can never carry a remote URL.
// emoji = the project's AI-picked 1–3 emoji identity badge; "" until the
// server's emoji_sweep has badged it (or when the naming gateway is off).
```

### sessionMeta
```jsonc
{ "cid", "pid", "title", "desc", "tab", "named":bool, "busy":bool, "waiting":bool,
  // tab = AI 1-2 word label for the tab strip ("" until the namer runs; UI falls back to title)
  // promptedAt = epoch of the last HUMAN prompt (tab-strip age; 0 for pre-field sessions —
  // consumers fall back to lastActive, which every hook bumps incl. restart resumes)
  "tool":<str|null>, "status":"blocked|working|idle", "digest":str,
  "blocked_on":str, "sessionId", "promptCount":int,
  "lastActive":float, "created":float, "alive":bool, "account":str,
  "pinned":float }
  // pinned = epoch when the session was 📌 parked on the pin board (see the `pin`
  // op); 0.0 = not pinned. Pinned sessions stay fully alive/promptable.
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
- `account` = which subscription account this session's claude runs under
  (recorded at spawn; `"default"` = the machine's plain `~/.claude` login).

### accountMeta
```jsonc
{ "name", "email", "status":"ready|pending", "active":bool,
  "usagePct":<float|null>, "headroom":<float|null>,
  "windows":[{ "key", "label", "used":float, "resets":<iso|null> }...],
  "checkedAt":<float|null>, "error":str, "configDir":str }
```
- `status:"pending"` = the account dir exists but no credentials yet (the
  sign-in ceremony hasn't been completed). It flips to `ready` on its own once
  the harness observes credentials (~15s poll).
- `usagePct` = the most-constrained usage window (0–100, from Claude's OAuth
  usage endpoint — **undocumented**, so `null`/stale data must degrade to "no
  opinion"). `headroom` = `100 − usagePct`. `active` = new sessions spawn here.
- `auto` (top-level) = the harness's local usage-aware auto-switch is on
  (hysteresis + debounce; see `docs/fleet/SUB-ROUTING.md`).

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
