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

## Autonomy (the write guard)

Write verbs (assign / ask / answer_prompt / interrupt / create|clone project)
pass through a gate; reads are always free.

- `readonly` — refuse writes, return a proposal instead.
- `confirm` (default) — a write returns `needs_confirm`; re-call with
  `confirm=true`. In chat the bot proposes, you say yes, it acts.
- `auto` — execute immediately.

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

Read: `get_world`, `get_attention`, `session_digest`, `open_session`,
`open_project`, `list_tasks`, `get_task`.
Write: `create_task`, `set_task_status`, `note_task`, `assign`, `ask`,
`answer_prompt`, `interrupt`, `create_project`, `clone_project`.

Sessions are addressed by `(machine, cid)`, tasks by id. `get_attention` is the
triage entry point — each item names the `suggested_action` to clear it.

## Config (env, or inherited from `.clawd-harness.env`)

| var | default | meaning |
|---|---|---|
| `CONTROLLER_HARNESS_WS` | `ws://127.0.0.1:8787` | harness to drive |
| `CONTROLLER_HARNESS_TOKEN` | `.clawd-harness.token` | WS token |
| `CONTROLLER_HARNESS_HTTP` | (derived from WS url) | UI origin for deep links |
| `CONTROLLER_MODEL` | (Claude Code default) | pin the PM's `claude --model` |
| `CONTROLLER_AUTONOMY` | `confirm` | write gate |
| `CONTROLLER_CHAT_PORT` | `8799` | chat UI port |
| `CONTROLLER_LEDGER` | `../.clawd-controller.tasks.jsonl` | task log |
| `CONTROLLER_TELEGRAM_TOKEN` | — | bot token (a dedicated, un-polled bot) |
| `CONTROLLER_TELEGRAM_ALLOW` | `672968601` | csv of allowed Telegram user ids |

## Tests

```bash
python3 -m controller.test_controller   # client → world → verbs (mock harness)
python3 -m controller.test_mcp          # MCP dispatch (read + write)
python3 -m controller.test_mcp_stdio    # MCP as a real stdio subprocess
python3 -m controller.test_threads      # PM conversation threads (store + persist)
```

## Files

`harness_client.py` (WS client + state) · `world.py` (snapshot + attention) ·
`ledger.py` (event-sourced task log) · `verbs.py` (intent verbs + guard) ·
`mcp.py` (MCP stdio server) · `agent.py` (the PM brain: a minimal claude-p-agent,
`claude -p` + fleet tools) · `prompts/` (the persona, by trust level) ·
`events.py` (Reactor: hooks → higher-level events) · `telegram.py` (Telegram
bridge) · `chat_server.py` + `chat.html` (chat UI) · `threads.py` (PM conversation
threads) · `mock_harness.py` (test double) · `__main__.py` (entry).
