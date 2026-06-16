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

## The two brains (switchable live in the UI header)

Both drive the **same tools** and share the **same task ledger** file, so you can
switch mid-conversation:

- **`bankr`** (default) — Kimi K2.6 via the Bankr gateway, in-process. A JSON
  action loop (model-agnostic, no native function-calling needed). Fast, cheap.
- **`claude-code`** — shells out to `claude -p` with the controller's MCP server
  attached, so Claude Code itself is the agent. Heavier, but full Claude.

Set the startup default with `CONTROLLER_BRAIN=bankr|claude-code`.

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

## The tool surface (read + write)

Read: `get_world`, `get_attention`, `session_digest`, `list_tasks`, `get_task`.
Write: `create_task`, `set_task_status`, `note_task`, `assign`, `ask`,
`answer_prompt`, `interrupt`, `create_project`, `clone_project`.

Sessions are addressed by `(machine, cid)`, tasks by id. `get_attention` is the
triage entry point — each item names the `suggested_action` to clear it.

## Config (env, or inherited from `.clawd-harness.env`)

| var | default | meaning |
|---|---|---|
| `CONTROLLER_HARNESS_WS` | `ws://127.0.0.1:8787` | harness to drive |
| `CONTROLLER_HARNESS_TOKEN` | `.clawd-harness.token` | WS token |
| `CONTROLLER_MODEL` | `kimi-k2.6` | bankr brain model |
| `CONTROLLER_BRAIN` | `bankr` | startup backend |
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
```

## Files

`harness_client.py` (WS client + state) · `world.py` (snapshot + attention) ·
`ledger.py` (event-sourced task log) · `verbs.py` (intent verbs + guard) ·
`mcp.py` (MCP stdio server) · `brain.py` (Kimi brain) · `claude_brain.py`
(Claude Code `-p` brain) · `events.py` (Reactor: hooks → higher-level events) ·
`telegram.py` (Telegram bridge) · `chat_server.py` + `chat.html` (chat UI) ·
`mock_harness.py` (test double) · `__main__.py` (entry).
