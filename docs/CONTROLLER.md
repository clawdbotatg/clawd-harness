# The AI controller — a project-manager layer over the fleet

> Status: **built end-to-end** (Phases 0–2, plus the chat product). The reading
> phase lives in `server.py`; everything else lives in **`controller/`** (a
> harness client, never imports `server.py`). You can chat with the PM bot today
> at `http://127.0.0.1:8799` (`python3 -m controller serve`), on either of two
> brain backends (Kimi K2.6 / Claude Code `-p`), reading and writing the fleet
> through one MCP tool surface. See **`controller/README.md`** to run it.
>
> As-built map: `controller/harness_client.py` (WS client), `world.py`
> (snapshot + attention), `ledger.py` (event-sourced task log), `verbs.py`
> (intent verbs + autonomy/rate/audit guard), `mcp.py` (MCP stdio server),
> `brain.py` + `claude_brain.py` (the two brains), `chat_server.py` + `chat.html`
> (the chat UI), `mock_harness.py` + `test_*.py` (tests). UI: session cards in
> `index.html` now show `status`/`digest`/`blocked_on`; project cards show a
> "needs you" badge from `waitingCount`.

## The idea in one paragraph

We already drive N machines × N projects × N sessions from one place (the
fleet). Every session is a black box only a human reads today. The controller
is an **AI project manager that sits on top of the whole fleet**: it can see
the state of every session, knows what each one was *asked* to do, surfaces the
ones that need you, and — on command — spins up sessions and assigns work. You
talk to it in natural language ("add OAuth to slop-computer-live", "anything
waiting on me?") and it drives the fleet through the same WebSocket protocol a
browser speaks.

## The one principle: substrate vs. intent

Two layers, and the seam between them is load-bearing:

- **The harness is the execution substrate.** It owns sessions, PTYs,
  transcripts. It runs `claude`. It already describes itself well (titles,
  busy/idle/blocked state). It knows nothing about "tasks" or "goals" and never
  will.
- **The controller is the intent layer.** It owns the task ledger (what each
  session is *supposed* to accomplish), fleet-wide prioritization, and planning.
  It is **not part of the harness** — it's another client, exactly like the
  fleet worker. (Same discipline as `fleet/`: never import `server.py`, reach it
  only over the WS protocol in `docs/WS-PROTOCOL.md`.)

The dividing question for *where any given capability lives*:

> **Does it need to know about other sessions, or remember intent over time?**
> No → it's about *this* session, *now* → **harness-side** (richer meta).
> Yes → global view or persistent memory → **controller-side** (the brain).

## Two phases of the work

Becoming an AI-legible "PM surface" is two separable jobs:

1. **The reading phase** — make the surface easy for an AI to *understand*.
2. **The writing phase** — give the AI intent-level *verbs* to act.

---

## Reading phase — making the surface legible

### It's already an API

`index.html` is not the interface; it's *one renderer* of the WS protocol
(`docs/WS-PROTOCOL.md`). The fleet worker proves the protocol fully drives the
harness with zero pixels. So we don't build an API — we **reshape the existing
one** from "live-rendering / stream-shaped" toward "snapshot / intent-shaped".

The gap: the protocol is great for painting a live terminal and accumulating
state in a human's eyes. An AI PM wants the state **already reduced** — status,
goal, last exchange, blocker — not a transcript to re-read every tick. An AI
managing 20 sessions cannot re-ingest 20 transcripts per glance. So the design
pressure is **compression into semantic state**.

### How: deepen the meta the harness already emits (option 2)

The harness *already* runs a cheap LLM over each session — that's
`name_at_prompt` → `generate_name` → `_regenerate_name`, fired on the `Stop`
hook, producing `title` + `desc`. The reading phase is **the same machinery
doing more**: not a new system, a bigger label.

Two kinds of new meta, split by whether they need the LLM:

**Free facts (no LLM — derive from state/transcript we already have):**
- `status` — `blocked | working | idle` from the existing `busy`/`waiting`
  flags. (`error`/`done` are later refinements; `result.is_error` from the
  transcript can feed `error`.)

**LLM digest (the deepening of naming):**
- `digest` — one line: *what is this session doing right now*. Volatile.
- `blocked_on` — the actual open question, when `status == blocked` (else null).

### Cadence — why two clocks

- **Title/desc** stay on the milestone cadence (`name_at_prompt`: prompt 1, then
  every 5). They're a *stable label*; re-naming every turn makes titles flicker.
- **Digest/blocked_on** regenerate on **every `Stop`** — that's exactly when
  `data.last` is fresh and the state changed, and the digest is *meant* to be
  volatile. Each call is cheap (qwen3-coder); the user explicitly wants "more AI
  per session," so the extra calls are the point.

Implementation: a second system prompt (`DIGEST_SYS_PROMPT`) + `generate_digest`
sharing one HTTP helper with `generate_name` (single transport, no drift). A
`_regenerate_digest` method fired from the `Stop` branch of the hook handler.
`digest`/`blocked_on`/`status` added to `ClaudeSession.meta()` so they ride the
existing `sessions` broadcast — **every** client gets richer at once: the human
GUI, the fleet phone, and the controller.

### Persistence

None. The digest is *derived and ephemeral* — held in memory, regenerated next
turn. Title/desc keep persisting in the registry as today (they're a stable
label worth surviving a restart). This matches "zero persistence for derived
meta."

### Why harness-side here, controller-side elsewhere

Per-session description is paid **once** and read by everyone — the GUI gets
richer too, which a controller-side summarizer could never deliver. Cross-session
reasoning (the task ledger, "which of 20 needs you *most*", planning) needs a
global view + persistent intent the harness must not own → that stays in the
controller.

---

## Writing phase — intent-level verbs

The controller thinks in PM verbs, which **compile down** to the existing
keystroke-level actuators (`new`, `send`, `input`):

```
create_task(goal, project, acceptance) -> task_id   # ledger-only (controller state)
assign(task_id, {spawn_in: pid} | {existing: cid})  # → new + send
ask(cid, text)                                       # → send
answer_prompt(cid, choice)                           # → input (raw keys) — the hard one
interrupt(cid) / pause(cid)
session_digest(cid)                                  # deep-read one session on demand
```

**`answer_prompt` is the one that's genuinely harder than the rest.** A
`waiting` session is parked on a TUI menu; answering means raw arrow-keys+enter
via `input`, not text. It's the difference between a PM that *surfaces*
blockers and one that *clears* them — prototype it early.

---

## The controller (the brain)

A headless process that is **a relay client + a semantic projection + the task
ledger**, exposed as **MCP**:

- **MCP resources = the read shape**: `world` (the whole fleet as one compact
  object), `attention` (the derived "needs you" queue), `session_digest(cid)`.
- **MCP tools = the write shape**: the intent verbs above.

Packaging it as MCP means the PM brain can be *any* agent — including a stock
Claude Code session, or a scheduled cron agent — pointed at the fleet. The
meta-move: the controller could be **a session inside the harness itself**, a
Claude whose MCP tools manage all the *other* sessions; you'd talk to your fleet
manager in the same UI you use for everything else.

Architecturally the MCP server *is* a relay client (so it spans all machines),
obeying the same boundary as the worker: dials the relay, speaks the protocol,
never imports the harness.

### The `world` object (sketch)

```jsonc
{ "machines": [{ "id":"laptop", "status":"online",
  "projects": [{ "pid":"abc", "name":"slop-computer-live",
    "sessions": [{ "cid":"s1", "title":"wire up auth", "task":"T-14",
      "status":"blocked", "blocked_on":"Postgres or SQLite?",
      "last_assistant":"…need the DB before…", "digest":"wiring OAuth, blocked on DB",
      "idle_for_s":240 }] }] }] }
```

### Model strategy (tiered)

- **Compression** (digests, "is this a question for the human?"): tiny/cheap,
  high-frequency — the qwen3-coder tier, like naming.
- **PM reasoning** (planning, deciding to act): the strong model, low-frequency,
  high-stakes (it spends money and instructs other agents). Cheapness here is a
  false economy. (CLAUDE.md already flags this split.)

The cheap tier continuously **compresses** raw session activity into the world
model; the strong tier reasons over the *compressed* world, never the firehose.
That's the unlock for both legibility and cost.

## Multi-machine: the trusted-control path (as built)

The brain runs **on the box, next to the relay** (`clawd-nerve-cord` /
`zkllmapi`) — the box is the brain. But the box has *no harness of its own*, and
the relay only ever sees **ciphertext** (the phone⇄worker channel is E2E,
passkey-bound). A headless brain can't be a passkey-holding phone, so it can't use
that path. Instead it drives machines over a separate **trusted-control path**:

```
controller (box) ──role=controller──▶ relay ──task(__ctl__)──▶ worker ──▶ local harness
```

- **relay** (`fleet/relay.py`): a `role=controller` connection gated by a strong
  shared secret (`FLEET_CONTROLLER_TOKEN`) joins as a pre-authed mobile under the
  reserved ident `__ctl__`; the *existing* `toMachine`/`machineMsg` routing
  carries its control. No new wire protocol.
- **worker** (`fleet/worker.py`): **opt-in** per machine via `FLEET_CTL_ALLOW=1`.
  An opted-in worker bridges the reserved controller's **plaintext** harness
  frames to its local harness *even while E2E is required for mobiles*, and replies
  to it via the plaintext `reply()` (not the encrypting `reply_enc`), skipping PTY.
- **controller** (`controller/relay_client.py`): `RelayFleet` connects
  `role=controller`, demuxes the roster + per-machine `projects`/`sessions`/`hook`
  into a live `{machine: RelayMachine}` map that *is* the `clients` World/Verbs
  read. It re-pulls a machine's state on every worker (re)connect. Enabled by
  setting `CONTROLLER_RELAY` (box mode) instead of `CONTROLLER_HARNESS_WS` (the
  laptop's single-harness mode).

**The trade, stated plainly:** this makes the box a trusted component for *control*
(a compromised box could drive machines) — the deliberate "box = brain" choice.
The phone⇄worker **E2E is untouched**: the relay still can't read or forge that
traffic; only the opted-in trusted-control identity rides plaintext, and only on
machines that set the flag. Wiring a machine in: **[`fleet/ADD-MACHINE.md`](fleet/ADD-MACHINE.md)
step 8**.

---

## The task ledger — no database

The only genuinely new persistent state. It's tiny (tens of records) and the
whole stack is proud of being pure stdlib, disk-as-source-of-truth. So:

- **An append-only JSONL event log** (`.clawd-controller.tasks.jsonl`,
  gitignored, same family as `.clawd-harness.sessions.json`).
- The log **is** the history: `task_created`, `assigned`, `blocked`, `nudged`,
  `done`. Fold/replay it on boot to rebuild the ledger in memory (event
  sourcing; trivial at this scale).
- One file gives you three things: current state (replay), the **audit trail**
  (free), and time-travel (`grep`). Append-only also dodges mid-write corruption
  of a rewritten doc.
- **Upgrade path if ever needed:** `sqlite3` (stdlib, one file, no server) — only
  if you get concurrent writers or want indexed queries. Neither is true on day
  one. Postgres/a service: never.

## Guardrails (non-negotiable, because this amplifies money & mayhem)

- **Read-only by default.** The reading phase + attention queue ship with *zero*
  write capability and carry almost none of the risk.
- **Human-in-the-loop on every actuation, initially.** The controller *proposes*
  ("I'll tell s1: 'use Postgres' — ok?"); you confirm. Autonomy is earned later,
  per action-type.
- **Spend ceiling + rate limits + kill switch.** A controller that misreads idle
  as "needs a nudge" and spams `send` is a runaway; cap actions per session per
  window.
- **Audit log** = the JSONL ledger (what it saw → decided → sent).
- **Don't auto-answer permission prompts** unattended — the longest-manual thing.

---

## Phased plan

- **Phase 0 — reading phase / richer meta** ✅. `generate_digest` + `status` /
  `digest` / `blocked_on` on `meta()`; surfaced on the harness UI cards.
- **Phase 1 — world-model observer + attention queue** ✅. `harness_client.py`
  materializes the world; `world.py` derives the ranked "needs you" queue. Read
  via `python3 -m controller world|attention`.
- **Phase 2 — conversational controller** ✅. `verbs.py` (intent verbs, gated by
  autonomy/rate/audit) behind both an MCP server (`mcp.py`) and a chat brain
  (`brain.py` Kimi / `claude_brain.py` Claude Code). Confirm-gated by default.
- **Phase 3 — bounded autonomy** ◑. Autonomy gate + task ledger + an
  event-driven **Reactor** (`events.py`): fleet hooks (`Stop`/`Notification`)
  become higher-level events (`blocked`/`turn_done`/`ended`), pushed to handlers
  and `/api/notifications`. Remaining: a standing "verify a finished task against
  its acceptance" auto-loop.
- **Phase 4 — Telegram front-end** ✅. `telegram.py` — allowlisted, stdlib,
  routes messages to the same brain and lets the Reactor push `blocked` alerts to
  your phone. Enabled by setting `CONTROLLER_TELEGRAM_TOKEN` (a dedicated bot).

## Decision log

- **Richer meta lives in the harness, not the controller** — it's paid once and
  read by all clients (GUI included), and the LLM-per-session machinery already
  exists. Reversing my earlier "boundary smudge" worry: the harness *already*
  calls an LLM to name sessions, so this is a bigger dial, not a new job.
- **Task ledger lives in the controller, not the harness** — it needs a global
  view and persistent intent the harness deliberately lacks.
- **No database** — JSONL event log; sqlite only if concurrency/queries demand.
- **Controller = relay client + MCP**, never imports the harness — preserves the
  fleet boundary and lets any agent be the brain.
- **Tiered models** — cheap continuous compression, strong occasional reasoning.
