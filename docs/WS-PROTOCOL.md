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
  `sessions`, `hook`, `exit`, `reload`, `restart`, `irons`.

➡ For the fleet, this means: to let two phones watch two different sessions on the
same machine, the proxy worker opens **one harness WS connection per remote
viewer** (each with its own `client.cid`).

---

## Client → server (control frames)

| `type` | Fields | Effect |
|---|---|---|
| `subscribe` | `cid` | Attach to that session's live stream. Server immediately sends a `hello`, then a ring-buffer byte snapshot, then replays recent `transcript` history (see "On `subscribe`" below, incl. the unknown-cid error reply). |
| `list` | — | Server replies with `projects`, `sessions`, then `irons` snapshots. |
| `skillsLib` | — | 📚 skill library (DIRECT mode only — in fleet mode the browser sends this to the relay itself, not through a machine): the harness proxies to the relay's worker-token HTTP (`/skills/lib`, config from env / `fleet/fleet.env`) and replies (this client only) `{type:"skillsLib", skills:[{name, description, body}], error?}` — the user-written skill files stored on the relay, `body` the full SKILL.md text a tap pastes into the session. The library is deliberately decoupled from `~/.claude/skills` on any machine. Unconfigured/unreachable relay → empty list + explanatory `error`. `docs/fleet/SKILLS.md`. |
| `skillsRm` | `name` | 📚 ✕ (direct mode; same proxy): remove one skill from the library — trashed relay-side (`.clawd-fleet.skills/.trash/`), so recoverable by an admin — then replies the same fresh `skillsLib` frame. |
| `new` | `pid`, `account?`, `engine?` | Create a session in project `pid`, spawned under the ACTIVE subscription account (or the named `account` override). Server replies `{type:"focus", cid}` with the new id, and broadcasts `sessions`. `engine` picks the agent CLI — `"claude"` (default, and what an omitted field means) or `"codex"`; an unknown value falls back to claude. A non-claude engine ignores `account`: only claude participates in the subscription router. See docs/CODEX-ENGINE.md. |
| `accountAdd` | `name` | Register a new subscription account (config dir under `~/.clawd-accounts/<name>` + settings symlinks) and spawn its **sign-in session** — a normal claude in the self project under that `CLAUDE_CONFIG_DIR`, where the user completes the OAuth login. Replies `{type:"focus", cid}` for the sign-in session; broadcasts `accounts`. Re-invoking on a still-pending account opens another sign-in session; a no-op on a ready one. |
| `accountUse` | `name` | Flip which account NEW sessions spawn under (manual switch; running sessions untouched). Refused for a pending account. Broadcasts `accounts`. |
| `accountRemove` | `name` | Drop an account from the routing roster. Logs nothing out (config dir + Keychain credential stay; running sessions keep their recorded dir). Refused when it would leave no ready account; removing the ACTIVE account re-routes new spawns to the most headroom. `default` stays removed (it's only re-injected on an empty roster; `accountAdd` with name `default` re-adopts it, sans ceremony). Broadcasts `accounts`. |
| `accountsRefresh` | — | Poll every account's usage now (instead of waiting out the TTL). Broadcasts `accounts` on change. |
| `close` | `cid` | Kill that session (SIGTERM) and detach viewers. Files on disk untouched. |
| `pin` | `cid`, `on?` | 📌 park a session on the pin board (`on:true`, the default) or restore it to the tabs (`on:false`). Pure metadata — the claude process is untouched and can still be prompted via `send`; the UI keeps pinned sessions off the tab strip/sessions rung and shows them on the board instead. Persisted (survives restarts); broadcasts `sessions`. **Pinning has two side effects on the session itself:** it derives `testHint` (below), then types the engine's `/compact` into the TUI — parking is when compaction is free, so the return trip gets a full context window. The compact waits for the session to go genuinely idle (never mid-turn, never while a TUI prompt is up) for up to `PIN_COMPACT_WAIT` (900s), and is skipped for ceremony/never-prompted sessions or with `PIN_COMPACT=0`. It is sent as a *control* send: no `prompted_at` bump, no bounce watchdog, no `promptCount` change. |
| `autopilot` | `cid`, `on?` | 🤖 engage (`on:true`, the default) or disengage autopilot on a session — the checkbox beside the state square. While engaged, every `Stop` runs an LLM supervisor over the session's goal + transcript tail: it either types the next goal-directed prompt into the session itself (`via:"pilot"`, through the same routing preflight as a human send) or parks with a reason; `pilotStatus` (below) narrates each round. Engaging derives the goal and — if the session is idle — acts immediately. Bounded: at most `PILOT_MAX_ROUNDS` (20) pilot sends per human prompt (any non-pilot send refills the budget); ceremony sessions refuse to engage; a supervisor "prompt" starting with `/` is dropped, never typed. Durable (registry + respawn clone); broadcasts `sessions`. `AUTO_PILOT=0` opts the box out; model: `PILOT_MODEL` (default `claude-haiku-4.5` on the naming gateway). |
| `tldr` | `cid`, `on?`, `mark?` | 🟦 `mark:true` = the viewer tapped the summary: read this far — the server forgets the current summary and later passes cover only prose streamed after this point (blank `tldr` frame goes out). Otherwise: this viewer wants (`on:true`, the default) or no longer wants the **live TLDR** of a session: a rolling plain-English summary of the turn in flight, streamed as `tldr` frames (below) to that session's subscribers. Volatile — a browser re-sends it right after every `subscribe`. Turning it on mid-turn catches up on the text streamed so far; after a turn, one final pass over the last reply. Source is the API tee (`server.py` `API_TEE`: the session's `ANTHROPIC_BASE_URL` points at a local pass-through proxy that copies assistant text deltas as they stream); summarizer is one `claude -p --model haiku` (`TLDR_MODEL`) at a time under the session's account. Claude engine only. |
| `createProject` | `name` | Create a new public GitHub repo under `GH_OWNER` and adopt it (async; status broadcasts via `projects`). |
| `addProject` | `repoUrl` | Clone a repo and adopt it (async). Input normalized: full URL as-is; `owner/repo` and bare `repo` resolved against github.com. |
| `addLocalProject` | `path`, `create?` | Register a folder anywhere on the machine's disk (absolute or `~` path) as a **private local project** (`kind:"local"`): sessions run inside it like any project, but the harness never runs gh/git-remote operations on it and never stores/broadcasts a repo URL. Synchronous — the project appears `ready` immediately. A path that **doesn't exist** is answered with `localProjectMissing` (the resolved absolute path) instead of an error, so the UI can confirm with the human; the retry with `create:true` then `mkdir -p`'s the folder and registers it. Rejected (with an `error` frame to the sender) for: paths that exist but aren't directories, `/` or `~` itself, paths under `projects/` (auto-managed) or the harness's own dir (the pinned self-project) — all guards run before any mkdir, so `create` can't be aimed at them. Re-adding the same resolved path is a no-op. |
| `addExternalProject` | `repoUrl` | Adopt SOMEONE ELSE'S GitHub repo as an **external project** (`kind:"external"`). Async: the provisioning thread runs `gh repo view --json viewerPermission,defaultBranchRef,…` and decides — no push access → `gh repo fork <url> --clone` (origin = our fork under `GH_OWNER`, `upstream` = the source); push access → plain clone + an `upstream` remote pointing at the same repo, so every external project has the same remote shape. Records `upstream` + `defaultBranch`. Every session spawned in it is born with a standing rule (never commit/push the default branch; branch from `upstream/<default>`; push the branch to origin; `gh pr create --repo <upstream>`; report the PR link), and the server fast-forwards the default branch from upstream before each spawn so sessions never start stale. Lives under `projects/`, so removal is the delete-the-folder contract. Non-GitHub input is rejected with an `error` frame to the sender; re-adding a URL already cloned as a plain `gh` project converts it in place. |
| `removeProject` | `pid` | Detach a `kind:"local"` project: drop its registry entry and close its sessions. **Never touches the folder on disk.** Silently ignored for gh projects (they keep the delete-the-folder contract below) and the pinned self-project. |
| `ironCreate` | `title`, `desc?`, `tags?`, `pid?` | 🔥 create an **iron** — a named group of projects tracked as one effort (tapping an iron dives straight into its warmest session, with a one-row chrome over the scoped tab strip; only a **sessionless** iron lands on its page, `#/i/<id>`, member project rows each with a ＋ spawn). Title trimmed/clipped (80 chars; blank refused), `desc` ≤400, `tags` ≤8×24 — though the UI sends a **title only**: the desc is AI-derived (`ironDescribe` below) and tags are edited later, never at creation. Optional `pid` assigns that project into the new iron in the same op (the UI picker's create-and-add). Persisted in the registry; broadcasts `irons`. **Direct mode only** — in fleet mode irons span machines, so the browser stores them relay-side (the relay `prefs` frame carries an `irons` field; member refs are projectKeys there, pids here) and these ops are never sent. |
| `ironUpdate` | `id`, `title?`, `desc?`, `tags?` | Edit an iron's metadata in place (absent fields unchanged; blank title ignored). Broadcasts `irons`. |
| `ironDelete` | `id` | Delete an iron. Its projects and their sessions are untouched — the grouping is pure metadata. Broadcasts `irons`. |
| `ironAssign` | `pid`, `iron` | Put project `pid` into iron `iron` (**one iron per project** — assignment removes it from any other), or take it out of every iron with `iron:""`. Unknown pid/iron refused silently. A project dropped by the disk reconcile or `removeProject` is forgotten by every iron automatically. Broadcasts `irons`. |
| `ironOrder` | `ids` | Persist a drag-reorder of the irons list: `ids` (top first) becomes the priority order — each named iron's `rank` is set to its index. Unknown ids skipped; irons the list missed keep their old rank. The `irons` broadcast always arrives rank-sorted (a new iron is created at the top — smallest rank); an identical order is a quiet no-op. **Direct mode only** — fleet order is simply the array order of the relay `prefs` frame's `irons` field. |
| `ironDescribe` | `id`, `context` | **Stateless LLM call, both modes** (unlike the `iron*` ops above): derive an iron's one-line description from `context` (the browser-built summary of the iron's title, member projects and session titles, ≤6000 chars). Threaded; replies to the sender only with `{type:"ironDesc", id, desc}` — `desc` is `null` when naming is unconfigured / the call failed (the client keeps what it had). Runs on `IRON_DESC_MODEL` (default `claude-haiku-4.5`), falling back to `BANKR_MODEL` if that id isn't on the gateway. The *browser* stores the result (relay `prefs` push in fleet, `ironUpdate` here) and throttles re-asks (hourly, or on membership change, only while an iron surface is on screen) — the harness holds no iron state for this in fleet mode. |
| `input` | `data`, `cid?` | Raw keystrokes → PTY. `data` is a UTF-8 string (incl. escape seqs for TUI menus). Falls back to `client.cid` if `cid` omitted. |
| `send` | `text`, `cid?`, `via?` | High-level: type `text`, wait for the paste to settle, then submit `\r`. Use this to "send a message/prompt". Optional `via` tags the send's origin (`"quick"` = a quick-prompt chip tap) for the server's prompt log; omitted = typed. |
| `resize` | `cols`, `rows`, `cid?`, `claim?` | A size CLAIM, not a command — one PTY serves many differently-sized viewers, so the server sizes it to a single OWNER. `claim:true` (a deliberate act on that device: opening the tty view, resizing the window) takes ownership; without it the size only applies if the sender already owns the PTY (or nobody does). `input`/`send` from a sized viewer also takes ownership (you drive it, it fits you). `cols`/`rows` of `0` releases the claim (viewer left the tty view / went hidden) — ownership falls back to the most recently sized remaining viewer. Applied changes broadcast `ttySize`. Pre-policy servers treat every frame as an unconditional resize, so old/new mixes degrade to the last-write-wins behavior. |
| `restart` | `reason?`, `force?` | Request a graceful self-restart. Fires once nothing is **mid-turn** — a genuinely busy turn or live background work; a session merely `waiting` on an interactive prompt is parked on a human and does NOT hold it. `force:true` (the banner's **restart now**) fires immediately, cutting in-flight turns. A pending restart also self-fires after `RESTART_MAX_WAIT` (20 min). |
| `restartCancel` | — | Cancel a pending restart. |
| `ping` | `id?` | Liveness probe. Server immediately replies `{type:"pong", id}` (echoing `id`). Lets a client prove the *full* path is live (in fleet: browser→relay→worker→harness and back over the e2e channel) before deciding whether to repaint in place vs. tear down + re-subscribe. A pre-`pong` harness just ignores it — the prober falls back to reconnect on timeout, so it's backward-safe. |
| `search` | `q`, `scope?`, `limit?`, `id?` | **Controller read query.** Bounded server-side search over live sessions — meta fields (title/desc/tab/digest/blocked_on/lastAnswer; zero I/O) and/or the tail of each session's transcript (`scope`: `meta` \| `transcript` \| `all`, default `all`). Replies `{type:"searchResult", id, q, matches:[{cid,pid,title,lastActive,where,snippet}], scanned, truncated}` **to the sender only**, computed on a worker thread. Hard bounds: `limit` clamped to 40, ≤3 transcript hits/session (newest first), 160-char snippets, last 2MB of each transcript, 5s wall budget (→ `truncated:true`). Sessions scanned most-recently-active first. |
| `transcriptTail` | `cid`, `n?`, `chars?`, `id?` | **Controller read query.** Last `n` slim transcript events for one session (same `event` shape as `transcript` frames), every text field truncated to `chars`. Replies `{type:"transcriptTailResult", id, cid, events:[...]}` to the sender only; unknown cid → same frame with `error`. Clamps: `n`≤50, `chars`≤2000, whole reply ≤16KB (oldest events dropped). |
| `screen` | `cid`, `chars?`, `id?` | **Controller read query.** De-ANSI'd tail of the session's live terminal (ring buffer) — what a human sees right now; the way to read TUI dialogs (trust prompts, menus) that never reach the transcript. Replies `{type:"screenResult", id, cid, text, cols, rows}` to the sender only. `chars` clamped to 4000. |

The three **controller read queries** carry an optional `id` echoed in the
reply for request/reply correlation. A harness that predates them simply never
answers — callers should time out and degrade (the controller's `find` falls
back to meta-only for that machine). In fleet mode they ride the existing
worker/relay bridge untouched (any typed frame is forwarded verbatim for the
trusted `__ctl__` ident).

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
{ "type":"irons", "irons":[{ "id","title","desc","tags":[..], "pids":[..], "created", "rank" }...] }  // rank-sorted: list order = priority (see ironOrder)
{ "type":"skillsLib", "skills":[{ "name","description","body" }...], "error"? }  // reply to skillsLib/skillsRm (this client only) — 📚 library picker
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
{ "type":"tldr", "cid", "text", "final":bool, "turn":int }   // 🟦 to that session's subscribers (see the `tldr` verb):
                                                       // the latest live summary of the turn in flight; final=true is the
                                                       // tightened post-Stop pass; text="" on a new prompt = blank it.
                                                       // Also sent once on subscribe when a summary exists.
{ "type":"focus", "cid" }                              // reply to a "new" you sent
{ "type":"exit", "cid" }                               // the claude process for cid exited
{ "type":"ttySize", "cid", "cols":int, "rows":int }    // to that session's subscribers: the PTY was re-sized to a viewer's claim (see `resize`)
{ "type":"projects", "projects":[...], "boot" }        // re-broadcast on any project change
{ "type":"sessions", "sessions":[...], "current" }     // re-broadcast on any session change
{ "type":"accounts", "accounts":[...], "active", "auto" }  // subscription logins/usage/active changed
{ "type":"reload" }                                    // index.html changed on disk → browser should reload
{ "type":"restart", "pending":bool, "reason", "busy":int,        // restart state (banner)
  "waitedFor":float, "maxWait":float,                            // seconds pending / ceiling
  "blockers":[{ "cid", "title", "bg" }...] }                     // WHAT is holding it
{ "type":"restart", "state":"go" }                     // restart firing now (process about to exit)
{ "type":"localProjectMissing", "path" }               // to the sender of an addLocalProject whose
                                                       // path doesn't exist: the resolved absolute
                                                       // path — confirm, then retry with create:true
{ "type":"irons", "irons":[...] }                      // re-broadcast on any iron op and when the
                                                       // reconcile/removeProject drops a member pid
                                                       // (same shape as the on-connect snapshot)
{ "type":"ironDesc", "id", "desc" }                    // reply to the sender's ironDescribe (desc null
                                                       // = naming unconfigured/failed; "" = throttled)
```

### Read-query replies (to the requesting client only)
```jsonc
{ "type":"searchResult", "id", "q", "matches":[
    { "cid", "pid", "title", "lastActive":float,
      "where":"title|desc|tab|digest|blocked_on|lastAnswer|transcript",
      "snippet":"…≤160 chars around the hit…" } ],
  "scanned":int, "truncated":bool }
{ "type":"transcriptTailResult", "id", "cid", "events":[<event>...] }   // or { …, "error" }
{ "type":"screenResult", "id", "cid", "text", "cols":int, "rows":int }  // or { …, "error" }
```

---

## Object shapes

### iron (and the fleet `prefs` form)
```jsonc
// direct mode — the harness `irons` frames above:
{ "id", "title", "desc", "tags":[..], "pids":[..], "created":float, "rank":float }
// fleet mode — the harness never sees irons; the browser stores the list in the
// RELAY's `{type:"prefs", irons:[...]}` frame (both directions), one entry:
{ "id", "title", "desc", "tags":[..], "keys":[..], "created":float }
// `keys` (NOT pids): the fleet projectKeys the UI routes on, so a repo groups
// once across machines. No rank — fleet priority is simply array order. The
// relay sanitizes on write (clean_irons in fleet/relay.py: ≤64 irons, id ≤64,
// title ≤80, desc ≤400, tags ≤8×24, keys ≤256×512, all code-point clipped) and
// the browser mirrors that sanitizer exactly (index.html [irons-sanitizer]) —
// its echo-ack depends on the two agreeing (fleet/test_irons_parity.py).
```

### projectMeta
```jsonc
{ "pid", "name", "path", "repoUrl", "status":"ready|cloning|error",
  "error", "sessionCount":int, "busyCount":int, "waitingCount":int,
  "created":float, "pinned":bool, "kind":"gh|local|external", "lastTouched":float,
  "emoji":str, "upstream":str, "defaultBranch":str }
// upstream/defaultBranch: set only for kind:"external" — upstream is the SOURCE
// repo (where PRs go); repoUrl is where we push (our fork, or the source itself
// when we hold push access). Both "" on every other kind.
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
  "tool":<str|null>, "status":"blocked|working|background|idle", "bg":"shell|agent|", "digest":str,
  "blocked_on":str, "lastAnswer":str, "sessionId", "promptCount":int,
  "lastActive":float, "created":float, "alive":bool, "account":str,
  "pinned":float, "testHint":str, "engine":"claude|codex",
  "autopilot":bool, "pilotStatus":str, "pilotRounds":int }
  // pinned = epoch when the session was 📌 parked on the pin board (see the `pin`
  // op); 0.0 = not pinned. Pinned sessions stay fully alive/promptable.
  // engine = which agent CLI drives it. Absent on pre-2026-08 rows/clients —
  // treat a missing value as "claude". `account` is meaningful only for claude;
  // other engines sit outside the subscription router (docs/CODEX-ENGINE.md).
```
- `waiting` = the session is blocked on an interactive TUI prompt (a permission
  request, `AskUserQuestion`, or `ExitPlanMode`) and needs a human answer — it
  looks idle from the outside but isn't. `busy` is still `true` while `waiting`
  (the turn is in flight, just parked). In the **projectMeta** counts a waiting
  session is tallied in `waitingCount` *instead of* `busyCount` (mutually
  exclusive) so a blocked session reads as "needs you", not "working".
- `status` = the deterministic, LLM-free roll-up of `busy`/`waiting` for a
  controller's attention queue: `blocked` (needs a human now) > `working` (turn
  in flight) > `background` (turn over, but claude still has background work
  running) > `idle`.
- `bg` = what that background work is: `"shell"` (a `run_in_background` shell
  is still running) or `"agent"` (background subagents keep claude internally
  busy between turns); `""` when there is none. Detected by reading the status
  claude itself publishes to `<config_dir>/sessions/<pid>.json` (undocumented —
  the harness degrades to `""` if the file/field ever disappears). Jobs the
  session's claude deliberately detached (`nohup … & disown`) are invisible
  even to claude and are NOT reflected here. `bg` is only ever set while
  `busy` is false; UIs should treat it as "quietly working — don't disturb,
  but don't call it idle."
- `digest` = a volatile one-line "what this session is doing right now" (LLM,
  refreshed on every `Stop` — see naming below). `""` until the first turn ends
  or if naming is unconfigured. The *stable* label is `title`/`desc`; the digest
  is the live state. Held in memory only (not persisted; regenerated each turn).
- `testHint` = the **human's** verification step for a 📌 pinned session — one
  imperative line ("run a stream with three guests and watch for choppy
  video/audio"), LLM-derived from the transcript when the session is pinned and
  refreshed on each `Stop` while it stays pinned. Pinning means "coded, not yet
  verified", so this is what closing the to-do actually requires. `""` when the
  session isn't pinned (cleared on unpin), while the first generation is in
  flight, if the model judged there's nothing verifiable yet, or if naming is
  unconfigured. **Durable** (persisted in the registry) — a restart must not
  blank the board's instructions. Model: `TEST_HINT_MODEL`, defaulting to
  `BANKR_MODEL`.
- `autopilot` / `pilotStatus` / `pilotRounds` = the 🤖 autopilot (see the
  `autopilot` op): whether the harness's LLM supervisor is driving this
  session, its live narration line ("▶ tests green, wiring the UI next ·
  round 3/20" / "🙋 needs a db choice" / "✅ shipped and verified" / "⏸ paused
  at the cap"), and how many pilot sends the current human budget has used.
  All durable — an engaged pilot survives restarts and account handoffs.
- `blocked_on` = the open question if the turn ended by asking the human
  something in plain text (LLM-inferred) — a *soft* block the `waiting` flag
  (TUI prompts only) misses. `""` when not blocked. These three feed the AI
  controller's read-model; see `docs/CONTROLLER.md`.
- `lastAnswer` = the last turn's assistant message, truncated to 280 chars —
  **durable**: captured on every `Stop` and backfilled from the transcript on
  resume, so it survives harness restarts and controller reconnects (the full
  500-char version still rides the `Stop` hook's `data.last`; anything longer
  is `transcriptTail`'s job). Absent on old servers ⇒ degrade to hook-fed
  capture.
- `cid` = stable console id (ours; survives claude's id rotation). **Address
  sessions by `cid`, never `sessionId`.**
- `sessionId` = claude's own id; rotates on compaction/resume.
- `account` = which subscription account this session's claude runs under
  (recorded at spawn; `"default"` = the machine's plain `~/.claude` login).

### accountMeta
```jsonc
{ "name", "email", "status":"ready|pending", "active":bool,
  "fable":<bool|null>, "routable":bool,
  "usagePct":<float|null>, "headroom":<float|null>,
  "windows":[{ "key", "label", "used":float, "resets":<iso|null> }...],
  "checkedAt":<float|null>, "walledUntil":<float>,
  "wallKind":"session|weekly|", "error":str, "configDir":str }
```
- `status:"pending"` = the account dir exists but no credentials yet (the
  sign-in ceremony hasn't been completed). It flips to `ready` on its own once
  the harness observes credentials (~15s poll).
- `usagePct` = the most-constrained usage window (0–100, from Claude's OAuth
  usage endpoint — **undocumented**, so `null`/stale data must degrade to "no
  opinion"). `headroom` = `100 − usagePct`. `active` = new sessions spawn here.
- `walledUntil` is a stronger, CLI-confirmed limit verdict (epoch seconds; `0`
  when inactive). The router excludes every login sharing that organization
  until the matching 5h/session or weekly reset even if the undocumented usage
  endpoint still reports a low percentage. `wallKind` names that reset class.
- `fable` = does this plan carry Fable? `true`/`false` from the usage payload's
  scoped weekly window, **`null` = not known yet** (never had a good reading) —
  consumers must treat `null` as "yes", never as "no". `routable:false` = the
  router skips this pool regardless of headroom (`SUB_REQUIRE_FABLE`) and
  evacuates idle sessions from it; the login is still fine and still manually
  selectable, so render it as out-of-rotation, **not** as signed out.
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
