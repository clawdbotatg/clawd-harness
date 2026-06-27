# For leftclaw — what changed & how to test

Short handoff from the claude-p-agent refactor work. **This laptop (`random-agent`) is unrelated** — do not symlink or wire it into harness.

---

## What we did (on GitHub)

### 1. [claude-p-agent](https://github.com/clawdbotatg/claude-p-agent) — the engine

- **`agent.py`** is the only place that spawns `claude -p` (`run_turn()`).
- Adapters pass channel policy via `append_system_prompt`, tools via `extra_args`, continuity via `session_id` / `--resume`.
- TUI got session resume across REPL turns (`33e2057`).

**Brain on leftclaw:** `~/clawd/clawd-harness/projects/claude-p-agent` (your clone with `CLAUDE.md` + tools). Not Austin's `random-agent`.

### 2. [clawd-video-chat](https://github.com/clawdbotatg/clawd-video-chat) — the face

- **`cc-bridge.py`** imports `run_turn()` from `CLAUDE_P_AGENT_HOME` (no duplicated spawn logic).
- Channel prompts live here: `prompts/voice.md`, `backchannel.md`, `voice-trusted.md`.
- **`slop-bridge.sh`** now checks **cc-bridge `:7861`**, not openclaw `:18789` (`c2a85f3`).

**Face on leftclaw:** `~/clawd/clawd-harness/projects/clawd-video-chat`.

### 3. [clawd-harness](https://github.com/clawdbotatg/clawd-harness) — orchestration

- **Rolled back** fleet/passkey/tty mess to pre-regression state (`9de77ca` → tree matches `7ddf0dd`, Jun 24).
- **Re-applied only** the controller adapter on top (`6ee01ed`): `controller/agent.py` calls `run_turn()` via `CLAUDE_P_AGENT_HOME` + fleet MCP. No fleet/e2e/passkey changes in that commit.

---

## ⚠️ Controller crash fix (`ModuleNotFoundError: agent`)

Commit `6ee01ed` made the PM import `agent.run_turn` from a **separate** claude-p-agent clone. If `projects/claude-p-agent/` doesn't exist on the box, the old code **crash-looped at import** (launchd KeepAlive).

**Fixed in harness tip after this doc:** lazy import — controller **starts** even without the brain clone; PM chat returns a clear error until the clone exists.

**Before PM chat or video works**, ensure the brain clone exists:

```bash
test -f ~/clawd/clawd-harness/projects/claude-p-agent/agent.py || \
  git clone https://github.com/clawdbotatg/claude-p-agent \
    ~/clawd/clawd-harness/projects/claude-p-agent
cd ~/clawd/clawd-harness/projects/claude-p-agent && git pull
# copy CLAUDE.md.example → CLAUDE.md and customize (leftclaw's persona, not Austin's random-agent)
```

Then re-install controller so launchd gets `CLAUDE_P_AGENT_HOME`:

```bash
cd ~/clawd/clawd-harness && ./daemon-controller.sh install   # or restart if plist already has the env var
```

---

## What leftclaw needs to do

You (Claude Code on leftclaw) already know the deploy drill. Roughly:

```bash
cd ~/clawd/clawd-harness && git pull
cd ~/clawd/clawd-harness/projects/claude-p-agent && git pull   # after clone exists
cd ~/clawd/clawd-harness/projects/clawd-video-chat && git pull
```

Restart anything that loaded old Python (controller at minimum; cc-bridge if it was running):

```bash
./daemon-controller.sh restart    # or launchctl kickstart -k gui/$(id -u)/com.clawd.controller
launchctl kickstart -k gui/$(id -u)/com.clawd.cc-bridge   # if installed
```

**Preflight:**

```bash
lsof -nP -iTCP:7861,8787,7900 -sTCP:LISTEN   # brain, harness, call UI (7900 only when slop-bridge up)
tail -f ~/.cache/clawd/cc-bridge.log
```

---

## Test plan

### A. Harness + controller (PM brain)

1. Open harness UI (`:8787`) — fleet loads, passkeys behave like pre-regression (one per machine, no storm).
2. Open controller chat (`:8799` or your port) — send a message; PM should reply using `run_turn()` (needs `projects/claude-p-agent` present and `CLAUDE_P_AGENT_HOME` default path valid).

### B. Video call stack

From `projects/clawd-video-chat`:

```bash
./slop-bridge.sh
```

1. Page loads at `:7900`, gateway connected to **`:7861`** (not openclaw).
2. Say **"okay clawd"** — wake word → reply spoken via ElevenLabs.
3. **Session memory:** tell it a magic number, then ask again on the next utterance — should remember (cc-bridge keeps `session_id` per `sessionKey`).

### C. Regression check (why we rolled back)

- Reload harness in browser — terminal should **render**, not go black.
- Cold reload fleet — **one passkey per machine**, not a batched Face ID storm.

---

## Env reminders (leftclaw only)

| Var | Where | Points to |
|-----|--------|-----------|
| `CLAUDE_P_AGENT_HOME` | cc-bridge plist, controller default | `~/clawd/clawd-harness/projects/claude-p-agent` |
| `CC_BRIDGE_CWD` | cc-bridge plist | same brain clone |
| `OPENCLAW_WS_URL` | video-chat `.env` | `ws://127.0.0.1:7861` |

See `clawd-video-chat/SERVICES.md` for the full port map (`7900` senses · `7861` thinks · `8787` codes · `7851` private backchannel).

---

## Commits to expect after `git pull`

| Repo | Tip (approx) | Note |
|------|----------------|------|
| clawd-harness | `72c1764`+ | rollback + controller cherry-pick |
| claude-p-agent | `33e2057` | engine + TUI resume |
| clawd-video-chat | `c2a85f3` | cc-bridge + slop-bridge fix |

If anything fails on import (`from agent import run_turn`), the brain clone is missing or stale — `git pull` in `projects/claude-p-agent`.
