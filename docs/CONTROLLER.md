# The AI controller — a project-manager layer over the fleet

> Status: **built end-to-end** (Phases 0–2, plus the chat product). The reading
> phase lives in `server.py`; everything else lives in **`controller/`** (a
> harness client, never imports `server.py`). You can chat with the PM bot today
> at `http://127.0.0.1:8799` (`python3 -m controller serve`). The PM brain is a
> minimal [claude-p-agent](https://github.com/clawdbotatg/claude-p-agent) — real
> Claude (`claude -p`, on your subscription) driving the fleet through one MCP
> tool surface. See **`controller/README.md`** to run it.
>
> As-built map: `controller/harness_client.py` (WS client), `world.py`
> (snapshot + attention), `ledger.py` (event-sourced task log), `verbs.py`
> (intent verbs + autonomy/rate/audit guard), `mcp.py` (MCP stdio server),
> `agent.py` + `prompts/` (the PM brain + its persona), `chat_server.py` + `chat.html`
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
assign(task_id, {spawn_in: pid} | {existing: cid}, engine)  # → new + send
ask(cid, text)                                       # → send
answer_prompt(cid, choice)                           # → input (raw keys) — the hard one
interrupt(cid) / pause(cid)
session_digest(cid)                                  # deep-read one session on demand
```

**`answer_prompt` is the one that's genuinely harder than the rest.** A
`waiting` session is parked on a TUI menu; answering means raw arrow-keys+enter
via `input`, not text. It's the difference between a PM that *surfaces*
blockers and one that *clears* them. As built it is no longer blind: the persona
requires reading the evidence first — `transcript_tail` (a pending
AskUserQuestion's options ride in its `tool_use` event) or `peek_screen` (a
de-ANSI'd render of the live terminal, for TUI dialogs that never reach the
transcript; the `screen` WS frame).

### Retrieval verbs (the 2026-08 senses rebuild)

The original read surface (bare `get_world` + `session_digest`) collapsed at
fleet scale: the raw snapshot serialized to 66KB — past the tool-output budget
of the very model reading it — and there was **no retrieval channel at all**
(no search, no transcript read; `last_answer` lived in process memory and died
on every reconnect). "Find the task about Gmail" cost a 20-Bash-call turn that
hit the timeout wall. The rebuilt surface:

- **`find(query)`** — ONE call answers "which session/task/project is about X":
  task ledger + cached session/project meta locally, plus a **server-side**
  transcript search fanned out to each machine (the `search` WS frame — hard
  caps: ≤40 matches, ≤3 hits/session, 160-char snippets, 5s budget per
  machine). Returns deep links; machines that can't answer are listed in
  `unreachable`, meta+ledger still cover them.
- **`get_world(machine=, pid=, verbose=)`** — compact by default (one line per
  session, empty projects as a name list, per-machine counts) and size-guarded
  in the verb: over ~20k chars it drops digests, then collapses to counts. It
  can never again exceed the tool budget. `verbose` only when scoped.
- **`transcript_tail(machine, cid, n)`** — last n slim transcript events
  (`transcriptTail` frame; n≤50, text truncated, ≤16KB reply): what a session
  actually said/did, and the way to retrieve a delegated session's result.
- **`sweep(max_items)`** — the one-call check-in bundle: attention queue
  enriched with a 3-event tail per high item, a deep link, and a suggested
  `clear_with` verb, plus rollups (`working`: sessions actively mid-turn —
  added 2026-08-17 after a sweep-only "how's everything?" answer read a busy
  fleet as quiet, since a working session is neither attention nor idle;
  idle-no-task sessions; in-progress tasks
  whose sessions are gone). Read-only; the persona's sweep protocol acts on it
  under the autonomy gate. `CONTROLLER_SWEEP_EVERY` optionally pushes a
  deterministic (LLM-free) digest to Telegram on a timer.
- **`sessionMeta.lastAnswer`** — the harness now persists each session's last
  Stop message in its broadcast meta and **backfills it from the transcript on
  resume**, so the cheapest retrieval channel survives restarts, reconnects,
  and per-turn subprocesses.

Wire contract for the three read frames: `docs/WS-PROTOCOL.md` (they ride the
existing worker/relay bridge untouched; old harnesses ignore them and callers
time out into graceful degradation).

### Pipelines — multi-step, multi-engine tasks (2026-08)

**The gap:** a PM turn ends when it replies, so "research it with claude, have
codex double-check that, then let claude write the final report" had nowhere to
live. The only unattended follow-up was the autopilot's verify turn, which is
single-shot, bound to one task↔one session, and explicitly forbidden from
spawning. Meanwhile `assign` — the persona's strong default for all new work —
could only ever spawn claude, so the engine layer was reachable from exactly one
verb (`spawn`) that the persona steers away from.

A **pipeline** is a task whose work is an ordered list of steps (≤6), each with
its own session and its own engine:

```
create_pipeline(goal, steps, acceptance)   # ledger-only; steps = [{role, engine, prompt, pid, reuse?}]
start_pipeline(task_id, machine, pid)      # WRITE — the ONE approval for the whole chain
advance_pipeline(task_id, force?)          # close the running step, run the next
get_step_output(task_id, n)                # a step's full recorded answer
```

Design decisions worth keeping:

- **Advancing is deterministic, not a PM turn.** `Autopilot._advance` calls the
  verb directly when a step's session finishes, so a long chain costs zero
  tokens and cannot be hallucinated into a different plan. It also *can't* be an
  LLM turn: the verify prompt is forbidden from spawning, and advancing is
  entirely about spawning the next step.
- **One approval, not one per step.** `start_pipeline` is the gated write; the
  steps it starts are the plan the operator approved, so `advance_pipeline` is
  gated only on `readonly` (still rate-limited and audited). It refuses to run
  step 1, so the gate can't be skipped by calling advance first.
- **The handoff is the session's final message.** Each step's kickoff carries
  every earlier step's answer (newest-first until the budget runs out, then
  presented chronologically) and says outright that its final message is what
  gets passed on.
- **`reuse: <n>`** sends a step to step *n*'s existing session instead of a
  fresh one — how "claude takes codex's feedback" keeps its own research in
  context. It's also the one sanctioned exception to the persona's
  never-reuse-a-session rule, because it's the same task's own session.
- **A baseline fingerprint per step.** A reused session already holds an answer;
  without recording its hash at step start, the advance logic would read that
  stale text as the new step's output and skip the work entirely.
- **The Stop hook is not a promise.** A codex session on a box running two
  harnesses can lose its turn signal outright (one shared hooks file — see
  `docs/CODEX-ENGINE.md`), and any dropped frame would wedge a chain forever, so
  a **settle sweep** (every `CONTROLLER_PIPELINE_SWEEP`s) advances a step whose
  answer is new, differs from the baseline, and has stopped changing for
  `CONTROLLER_PIPELINE_IDLE`s. A step whose session has vanished is force-closed.

Guarded by `controller/test_pipeline.py` (the full claude → codex → claude chain
runs itself against the mock harness, plus both no-hook fallbacks).

### Exposure — what the PM can see (2026-08)

Features kept shipping into the harness that the controller never learned about;
a verb the model can't see the effect of is a verb it never uses. Closed:

- **Engines.** `engine` now rides in `get_world` / `find` / `sweep` / `get_pins`
  — but **only when it isn't claude** (absent ⇒ claude, the wire's own
  convention, so the default costs no budget). `assign(engine=)` joins
  `spawn(engine=)`, and the ledger records which CLI ran which session.
- **`get_pins`** — the 📌 board: done-but-unverified work with each session's
  `test_hint`. A third queue that neither `sweep` (a pin isn't blocked) nor
  `idle_no_task` (a pin is deliberately parked) should report.
- **`get_accounts`** — per-machine subscription usage + the codex plan card.
  Read-only on purpose: the harness re-routes continuously and beats a
  turn-based PM at it, so the PM's job is to know and to warn.
- **Project kinds** — `kind:"local"` (a private folder: no gh, no remote, no
  repo URL) is visible in the snapshot, adoptable via `add_local_project`, and
  detachable via `remove_project` (which never deletes the folder and refuses gh
  projects).
- **Session states** — `background` and `pinned` are now explained in the
  persona; reading either as "idle work going undone" was the failure mode.

Guarded by `controller/test_engines.py`.

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

## Voice front-end — talk to the PM (2026-08-16)

The PM tab's **🎙 talk** button opens a live voice conversation with the fleet
PM: native speech-to-speech (OpenAI `gpt-realtime` over WebRTC), semantic VAD
turn-taking, barge-in. The recipe is the one verified in
[clawdbotatg/gpt-voice](https://github.com/clawdbotatg/gpt-voice)'s
`INTEGRATION.md`; the pieces here:

- **`controller/voice.py`** — the server half. `POST /api/voice/token`
  (reachable as `/pm/api/voice/token` through both proxies, so it inherits the
  relay's passkey gate) mints an ephemeral client secret; the real
  `OPENAI_API_KEY` never reaches the browser. The session config is minted
  fresh each time: a voice-tuned persona **with a live compact fleet snapshot
  and an identity brief from the clawd-md knowledge base baked in**, semantic
  VAD, input transcription (opt-in — omitting it silently kills user-side
  transcripts), and the tool definitions.
- **The tool split mirrors the model strategy**: the realtime model itself only
  *reads* — `whats_waiting` (sweep), `fleet_overview` (get_world), `find_it`,
  `check_pins`, `account_usage`, `read_lore` — all executed **client-side**
  against the same `/pm/api/tool` endpoint the debug page uses, so the voice
  layer can never do more than the chat surface allows. Anything that changes
  the world goes through **`ask_pm`** → `POST /pm/api/chat`, i.e. a real PM
  brain turn with the full verb surface, autonomy gate, rate limits, and audit
  ledger intact. The mint response carries an `exec` map (tool name → endpoint
  kind), so `voice.py` stays the single source of truth for the tool surface.
- **`/api/voice/lore`** — one bounded page of the clawd-md knowledge base
  (identity, lore, projects, infra, history) for the `read_lore` tool.
  `CONTROLLER_LORE_DIR` points at a checkout (default `~/clawd-md`); a missing
  checkout degrades to "no lore", never an error.
- **The client** (index.html): mic + `RTCPeerConnection` straight to OpenAI,
  events on the data channel, and after every tool result **two** events go
  back — `function_call_output` then `response.create`; without the second the
  model never speaks the answer. The session survives leaving the PM view (the
  fixed HUD — one glanceable state word — is the handle and the hang-up), so
  you can talk while walking the fleet. Transcripts land as ephemeral bubbles
  in the PM feed.
- **Knobs**: `OPENAI_API_KEY` (in `.env.controller` on the box) enables it;
  `CONTROLLER_VOICE_MODEL` (`gpt-realtime` default; `-mini` is ~3× cheaper),
  `CONTROLLER_VOICE` (marin default; realtime voices only — TTS-only names
  fail the mint), `CONTROLLER_VOICE_EAGERNESS`, `CONTROLLER_VOICE_SPEED`.
- **Constraints**: the mic needs a secure context (HTTPS or 127.0.0.1 — so in
  practice `h.atg.link`), and billing is per audio minute on the OpenAI key.
- **Tests**: `python3 -m controller.test_voice` (config shape, exec-map
  coverage, lore sandboxing, keyless 503) + `tools/voiceprobe.mjs` (the whole
  client loop against stubs).

## The task ledger — no database

The only genuinely new persistent state. It's tiny (tens of records) and the
whole stack is proud of being pure stdlib, disk-as-source-of-truth. So:

- **An append-only JSONL event log** (`.clawd-controller.tasks.jsonl`,
  gitignored, same family as `.clawd-harness.sessions.json`).
- The log **is** the history: `task_created`, `assigned`, `blocked`, `nudged`,
  `done` — plus `pipeline_created` / `step_started` / `step_done` for multi-step
  tasks. Fold/replay it on boot to rebuild the ledger in memory (event sourcing;
  trivial at this scale). Event sourcing is what makes a pipeline restart-safe:
  where a chain got to is a replay of its own log, not in-process state.
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
  autonomy/rate/audit) behind both an MCP server (`mcp.py`) and the PM brain
  (`agent.py` — a minimal claude-p-agent, `claude -p` + those tools).
  Confirm-gated by default.
- **Phase 3 — bounded autonomy** ◑. Autonomy gate + task ledger + an
  event-driven **Reactor** (`events.py`): fleet hooks (`Stop`/`Notification`)
  become higher-level events (`blocked`/`turn_done`/`ended`), pushed to handlers
  and `/api/notifications`. Remaining: a standing "verify a finished task against
  its acceptance" auto-loop.
- **Phase 4 — Telegram front-end** ✅. `telegram.py` — allowlisted, stdlib,
  routes messages to the same brain and lets the Reactor push `blocked` alerts to
  your phone. Enabled by setting `CONTROLLER_TELEGRAM_TOKEN` (a dedicated bot).
- **Phase 5 — senses + reliability rebuild (2026-08)** ✅. Root causes from the
  live box, fixed structurally:
  - **The world-flap bug**: `claude -p` spawns `python3 -m controller mcp` per
    turn; it used to dial the relay itself — a SECOND `role=controller` under
    the same reserved `__ctl__` ident, which the relay's same-ident supersede
    rule turned into a mutual connection-kill ping-pong for the whole turn
    (~1,020 reconnects/day, spawn focus-waits expiring, per-turn `last_answer`
    amnesia). Now the subprocess is a thin HTTP proxy to the serve process
    (`ProxyMCPServer` → `POST /api/tool`, opt-in via `CONTROLLER_MCP_PROXY`
    from `write_mcp_config`) — one fleet connection, ever. The serve link also
    sends its own WS pings (`KEEPALIVE_EVERY`) and logs every up/down with a
    reason.
  - **Retrieval**: `find` / `transcript_tail` / `peek_screen` / `sweep` +
    durable `lastAnswer` (see "Retrieval verbs" above).
  - **Turn policy**: one streaming path for HTTP + Telegram, watchdog-enforced
    (`CONTROLLER_TURN_STALL` 300s no-events kill, `CONTROLLER_TURN_TIMEOUT`
    1800s ceiling) — replaces the old 240s hard kill that ate every fleet-wide
    question. Live progress at `GET /api/turn`; structured `[turn]` start/end
    lines (duration, tools, outcome) in the journal.
  - **Context diet**: the brain's cwd is now its own home (`~/.clawd-controller/
    home`, outside the repo so the 22KB harness CLAUDE.md stays out of PM
    turns), with `controller/prompts/CLAUDE.pm.md` installed as its CLAUDE.md,
    engine memory in `~/.clawd-controller/memory`, auto-memory off by default,
    and (fleet mode) built-ins cut to `Read` (`CONTROLLER_PM_BUILTINS`).
  - **Framework re-sync** (claude-p-agent ≥ de70b99): the controller's
    duplicated subscription router is deleted — routing is the engine's
    `modules/router` hook, so the controller must never set `CLAUDE_CONFIG_DIR`,
    and `tools/module sync` is mandatory after pulling the engine repo. Engine
    pinned `engine="claude"` on every run_turn.
- **Phase 6 — the middle manager (2026-08)** ✅. The brain now works when the
  operator isn't talking to it (`controller/autopilot.py`):
  - **Event-driven turns**: Reactor events trigger budgeted PM turns —
    `blocked` → a TRIAGE turn (read evidence, clear trivial per the sweep
    protocol, else escalate); `turn_done` on a task-linked session → a VERIFY
    turn judging the work against the task's acceptance (the Phase-3 TODO,
    closed). Turns are serialized with operator chats by one shared lock and
    recorded into a visible "🤖 autopilot" chat thread.
  - **Runaway guards** (the reason this is safe to leave on): per-cid and
    per-task cooldowns, own-action echo suppression (via the audit ledger),
    hourly/daily turn budgets with a single starvation notice, hard pause
    under `autonomy=readonly`, and a persisted kill switch
    (`POST /api/autopilot {"on":false}`; knobs `CONTROLLER_AUTOPILOT*`).
  - **Escalation discipline**: the `escalate` verb is the PM's only unattended
    channel to the operator — `digest` items batch into ONE windowed Telegram
    push (`CONTROLLER_DIGEST_WINDOW`), `now` bypasses for genuine emergencies;
    the raw per-block reactor ping is suppressed while the autopilot owns it.
  - **Durable memory** (`controller/notes.py`): operator-set standing
    priorities + scoped fleet notes (`machine:` / `project:` / `task:` /
    `general`), written through `remember_note`/`set_priorities` verbs and
    rendered (bounded) into every turn's system prompt — the legible
    replacement for the auto-memory we turned off.

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
