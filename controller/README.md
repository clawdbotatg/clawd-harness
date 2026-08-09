# clawd-controller — the fleet PM

An AI **project manager** that sits on top of the harness/fleet. It sees every
session, remembers what each was asked to do, surfaces what needs you, and — on
command — starts work and unblocks sessions. You chat with it in a browser; it
reads and writes the fleet through tools.

It's a **client**, never part of the harness (same boundary as `fleet/`): it
dials the harness over the WS protocol (`docs/WS-PROTOCOL.md`) and never imports
`server.py`. Pure stdlib. Full design + rationale: **`../docs/CONTROLLER.md`**.

## Run

```bash
# chat UI + PM brain (needs the harness running on :8787)
python3 -m controller serve            # → http://127.0.0.1:8799
# or as a launchd daemon (always-on, restarts on crash):
./daemon-controller.sh install         # from the repo root

# one-shot inspection (no LLM, no browser):
python3 -m controller world            # the whole fleet as JSON
python3 -m controller attention        # the ranked "needs you" queue
python3 -m controller tasks            # the task ledger

# MCP stdio server — point any MCP client (Claude Code, cron) at this:
python3 -m controller mcp
```

## Debug / inspector page — `http://127.0.0.1:8799/debug`

(Also the 🛠 button in the PM drawer / chat header.) Three tabs:
- **Prompt** — the PM brain's persona (`prompts/private.md`, appended to
  `claude -p` each turn). Edit + Save to override it live (persisted to
  `.clawd-controller.prompt.txt`, survives restarts); Reset returns to the file.
- **Tools** — every tool the PM can call, with its schema, and a form to **run any
  of them yourself** and see the raw result. Writes still pass the autonomy gate
  (tick `confirm`).
- **What the PM sees** — the raw `world` / `attention` / `tasks` / `notifications`
  JSON the brain reads.

## The brain — claude-p-agent

Each turn shells out via **`run_turn()`** imported from claude-p-agent
(`CLAUDE_P_AGENT_HOME`). This adapter attaches the fleet **MCP server** and loads
persona from **`controller/prompts/`** (private for chat/Telegram, public for
untrusted adapters). Multi-turn continuity rides on `--resume`. See
[`agent.py`](agent.py).

The persona lives in **[`prompts/`](prompts/)**, picked by trust level:
- **`private.md`** — the trusted operator persona (chat UI + Telegram are *you*).
  Full, write-capable tools, gated by the autonomy guard.
- **`public.md`** — a locked-down read-only persona for any future untrusted
  adapter: it may only *read* fleet state, never act.

Pin a specific model with `CONTROLLER_MODEL` (e.g. `claude-sonnet-4.6`); empty →
Claude Code's own default.

## Conversation threads (the chat analog of per-project sessions)

The chat header has a **thread bar** — the PM equivalent of the harness's N
sessions per project. Run several PM conversations at once and switch between
them; each thread keeps its **own history** (and its own `--resume` id, so
continuity is preserved) plus its display transcript.

- **＋ new** — spawn a fresh thread (new context).
- **click a tab** — switch to that thread; its transcript reloads.
- **✕ on a tab** — *archive* it (hidden, restorable — click **🗄 N** to reveal
  archived threads, then click one to restore it).
- **🧹 clear** (header) — wipe the current thread's context but keep the slot.

Threads persist to `.clawd-controller.threads.json` (gitignored), so they — and
the brain history + `--resume` id inside them — survive a daemon restart. API:
`GET /api/threads`,
`GET /api/thread/messages?id=`, `POST /api/thread/{new,select,clear,archive}`.
Telegram shares the **current** thread.

**Thread bookkeeping must never wait on a turn.** One lock serializes every brain
turn (HTTP chat, Telegram, autopilot) — but ＋ new / select / archive are
deliberately *outside* it. They used to take it, and since a turn runs for
minutes, that made every control in the PM tab hang for the length of the turn:
the tab read as frozen, and each hung click also burned one of the browser's ~6
connections to the origin until the rest of the page stalled too. Safety instead
comes from `Threads`' own short-held lock plus `Router.chat` pinning its `tid`
before the turn, so `current` moving mid-turn can't misroute a transcript. The
one exception is **clear**, which drops brain memory a running turn is writing:
it returns **409** mid-turn rather than blocking. Pinned by
`test_pm_responsive.py` — don't put a thread endpoint back behind the turn lock.

## Autonomy (the write guard)

Write verbs (assign / ask / answer_prompt / interrupt / spawn / close / pin /
create|clone|add|remove project / start_pipeline) pass through a gate; reads are
always free.

- `readonly` — refuse writes, return a proposal instead.
- `confirm` (default) — a write returns `needs_confirm`; re-call with
  `confirm=true`. In chat the bot proposes, you say yes, it acts.
- `auto` — execute immediately.

**One exception, by design: `advance_pipeline`.** `start_pipeline` is the
approval for the *whole* chain, so the steps it starts don't each stop for a
second yes — otherwise a pipeline is just a slower conversation. It still refuses
under `readonly`, is rate-limited, audited like any write, and **refuses to run
step 1**, so the approval can't be skipped by calling advance first.

Plus a per-target rate limit (`CONTROLLER_RATE_PER_MIN`, default 8) and an audit
trail: every write appends an `action` event to the ledger.

## Telegram front-end (optional)

Talk to the PM from your phone. Set `CONTROLLER_TELEGRAM_TOKEN` to a bot token
**that isn't already being polled elsewhere** (Telegram allows one `getUpdates`
consumer per token — pointing it at a live bot 409s and disrupts it; the bridge
detects this and disables itself rather than fight). Allowlist senders with
`CONTROLLER_TELEGRAM_ALLOW` (default = Austin's id). Then `serve` starts the
bridge automatically: allowlisted messages route to the same brain; replies come
back as Telegram messages; `/reset` clears the chat.

## Higher-level events (hooks → reactions)

Every session's low-level Claude Code hooks (`Stop`, `Notification`, …) fan out
as WS `hook` frames; the controller's **Reactor** (`events.py`) watches them
across all sessions and fires *higher-level* events on transitions — a session
crossing into `blocked` (edge-triggered, deduped), a turn finishing, a session
ending. Handlers act on them: a `blocked` event **pushes a Telegram alert**, and
the full feed is at `/api/notifications` (shown in the chat UI's "Recent
events"). This is how a low-level hook causes a high-level reaction.

## Jump from the PM into a session ("take me there")

The PM can **send you straight into a session or project** in the harness UI.
Two ways, both backed by the read-only `open_session` / `open_project` verbs:

- **Just ask the PM** — "open the blocked one", "take me to the Force Regenerate
  session", "send me to the slop-computer-live project". The brain calls
  `open_session`/`open_project`, and the chat renders a big **↗ Open** button
  under its reply (the URL is in the reply text too, so it works from Telegram).
- **Click it in the "Needs you" panel** — every attention item carries an
  **open ↗** link straight to that session.

A verb returns the deep link three ways so any client can use it: an absolute
`url`, plus a host-relative `path` + `port` that the browser **rebuilds against
its own hostname** — so a link minted on `127.0.0.1` still works when you opened
the PM over the LAN from your phone. The link is the harness's own hash route
(`#/p/<pid>/s/<cid>` transcript, `…/tty` terminal, `#/p/<pid>` a project), so a
reload lands back on the same session. Pass `view:"tty"` for the terminal.

The HTTP origin is derived from the harness WS URL (`ws→http`); override with
`CONTROLLER_HARNESS_HTTP` if the UI lives at a different origin than the WS
endpoint (e.g. behind the relay).

## The tool surface (read + write)

Read: `get_world`, `find`, `transcript_tail`, `peek_screen`, `sweep`,
`get_attention`, `get_pins`, `get_accounts`, `session_digest`, `open_session`,
`open_project`, `list_tasks`, `get_task`, `get_step_output`.
Write: `create_task`, `set_task_status`, `note_task`, `assign`, `ask`,
`answer_prompt`, `interrupt`, `spawn`, `close`, `pin`, `create_project`,
`clone_project`, `add_local_project`, `remove_project`, `create_pipeline`,
`start_pipeline`, `advance_pipeline`.

Sessions are addressed by `(machine, cid)`, tasks by id. `find` is the retrieval
entry point ("which session is about X" in one call); `sweep` is the check-in
bundle; `get_attention` is the triage list — each item names the
`suggested_action` to clear it.

**A verb the persona doesn't mention is a verb that never gets used.** codex
shipped for a day reachable only from a `spawn` argument
`prompts/private.md` steered away from, so the PM never once started one. Adding
a capability means changing three things together: the verb, its MCP description
(the model reads those verbatim), and the persona.

### Engines

A session runs `claude` or `codex`, fixed at spawn: `spawn(engine=)`,
`assign(engine=)`, or per pipeline step. In every read shape, `engine` appears
**only when it isn't claude** — absent means claude, the same convention the wire
protocol uses for pre-engine harnesses, so the default costs no tool budget.
The ledger records which CLI ran which session.

### Pipelines (multi-step, multi-engine tasks)

For work that is a *chain* — research it, have something else check that, then
write it up. A PM turn ends when it replies, so this can't be driven
conversationally; the plan is recorded up front and executes itself.

```
create_pipeline(goal, steps)   # steps = [{role, engine, prompt, pid, reuse?}], ≤6
start_pipeline(task_id)        # WRITE — the ONE approval for the whole chain
advance_pipeline(task_id)      # normally automatic; by hand only to unstick one
get_step_output(task_id, n)    # a step's full answer (get_task truncates them)
```

Each step's kickoff carries every earlier step's final answer, so prompts are
written as "critique the research above". `reuse: <n>` sends a step to step *n*'s
own session instead of a fresh one — how "claude takes codex's feedback" keeps
its research in context, and the one sanctioned exception to the persona's
never-reuse-a-session rule.

The chain advances when a step's session finishes a turn — via a **direct verb
call from the autopilot, not an LLM turn**, so a long chain costs no tokens and
can't be re-planned mid-flight. A **settle sweep** covers a step whose turn-end
hook never arrives (see the codex caveat in `../docs/CODEX-ENGINE.md`): an answer
that is new and has stopped changing advances anyway, and a vanished session is
force-closed. Completion sets the task to `review` and pushes the report.

## Config (env, or inherited from `.clawd-harness.env`)

| var | default | meaning |
|---|---|---|
| `CONTROLLER_HARNESS_WS` | `ws://127.0.0.1:8787` | harness to drive |
| `CONTROLLER_HARNESS_TOKEN` | `.clawd-harness.token` | WS token |
| `CONTROLLER_HARNESS_HTTP` | (derived from WS url) | UI origin for deep links |
| `CONTROLLER_MODEL` | (Claude Code default) | pin the PM's `claude --model` (the debug page's **Config** tab overrides this live; override persists in `.clawd-controller.model.txt`) |
| `CONTROLLER_AUTONOMY` | `confirm` | write gate |
| `CONTROLLER_CHAT_PORT` | `8799` | chat UI port |
| `CONTROLLER_LEDGER` | `../.clawd-controller.tasks.jsonl` | task log |
| `CONTROLLER_TELEGRAM_TOKEN` | — | bot token (a dedicated, un-polled bot) |
| `CONTROLLER_TELEGRAM_ALLOW` | `672968601` | csv of allowed Telegram user ids |
| `CONTROLLER_PIPELINE_IDLE` | `120` | a pipeline step's answer must be unchanged this long before the settle sweep advances it without a turn-end hook |
| `CONTROLLER_PIPELINE_SWEEP` | `20` | how often that sweep looks |

## Tests

```bash
python3 -m controller.test_controller   # client → world → verbs (mock harness)
python3 -m controller.test_pm_senses    # find / bounded world / tail / sweep / proxy
python3 -m controller.test_engines      # what the PM can SEE: engines, pins, plans, kinds
python3 -m controller.test_pipeline     # the claude → codex → claude chain, self-driving
python3 -m controller.test_autopilot    # the middle-manager loop + its runaway guards
python3 -m controller.test_events       # Reactor: hooks → higher-level events
python3 -m controller.test_mcp          # MCP dispatch (read + write)
python3 -m controller.test_mcp_stdio    # MCP as a real stdio subprocess
python3 -m controller.test_threads      # PM conversation threads (store + persist)
python3 -m controller.test_pm_responsive # PM controls stay live while a turn runs
python3 -m controller.test_pm_naming    # AI thread titles + running tldr
python3 -m controller.test_empty_turn   # a silent turn gets one nudge, never '(no result)'
python3 -m controller.test_relay_fleet  # the trusted-control path through the relay
python3 -m controller.test_telegram     # Telegram bridge (mock Bot API)
```

No test needs a real `claude`, `codex`, relay or network — `mock_harness.py`
speaks the WS protocol and every engine is a fiction.

## Files

`harness_client.py` (WS client + state) · `world.py` (snapshot + attention) ·
`ledger.py` (event-sourced task log) · `verbs.py` (intent verbs + guard) ·
`mcp.py` (MCP stdio server) · `agent.py` (the PM brain: a minimal claude-p-agent,
`claude -p` + fleet tools) · `prompts/` (the persona, by trust level) ·
`events.py` (Reactor: hooks → higher-level events) · `telegram.py` (Telegram
bridge) · `chat_server.py` + `chat.html` (chat UI) · `threads.py` (PM conversation
threads) · `mock_harness.py` (test double) · `__main__.py` (entry).
