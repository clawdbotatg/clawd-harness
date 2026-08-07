# Running Codex sessions in the harness

> Status: **built (v1), 2026-08-07** — against `codex-cli 0.147.0`. A session is
> either `claude` or `codex`, picked at spawn from the ＋ row; everything else
> (projects, PTY view, busy pill, naming, pins, tabs, deep links, fleet) is
> engine-blind and unchanged.
>
> It's a smaller job than it looks because the harness never parses the
> terminal. It consumes three things, and Codex emits all three. The cost is
> concentrated in exactly two places: **scrollback** and the **subscription
> router** — see the gaps below for what that means in practice.
>
> ## What is verified, and what is not
>
> Measured on this machine (see "Verification" at the bottom for how):
> - ✅ hooks exist in 0.147.0 with claude-compatible payload field names
> - ✅ `--no-alt-screen` really does keep codex out of the alternate buffer
>   (`\x1b[?1049h` count: **0**) — so the ring replay lands in the normal buffer
> - ✅ spawn → PTY paints → `engine` in meta → `engine` persists to the registry
>   → a real `codex` child with the right argv → clean close
> - ✅ `CODEX_HOME` isolation, `hooks.json` install (0600)
>
> **Not yet verified — needs a signed-in codex** (`codex login` on the harness
> box; there were no credentials when this was written):
> - ❓ that the hooks actually FIRE and reach `/hook` (incl. `$HARNESS_CID`
>   surviving codex's shell invocation) — without this there's no busy pill
> - ❓ the rollout JSONL parse (`_slim_event_codex`) against a real transcript
> - ❓ send/submit timing (`SEND_SETTLE`) against codex's paste heuristic
> - ❓ whether scrollback is usable in practice (see gap 1)
>
> Until those are checked, a codex session is a working terminal whose
> *structured* channel is unproven.

## Why it's tractable

The harness's contract with a CLI is narrow (see CLAUDE.md, "Channels"):

1. **WRITE** — keystrokes into a PTY.
2. **READ (visual)** — raw PTY bytes → xterm.js.
3. **READ (structured)** — a transcript JSONL tailed off disk + lifecycle hooks
   POSTed to `/hook`.

We deliberately never parse the TUI's "weird text." That decision is what makes
a second engine a plug-in rather than a rewrite.

## The mapping

| harness need | `claude` | `codex` |
|---|---|---|
| interactive TUI in a PTY | `claude` (no `-p`) | `codex` (no `exec`) |
| per-account credential isolation | `CLAUDE_CONFIG_DIR` | **`CODEX_HOME`** (defaults `~/.codex`) |
| turn signal | hooks via `--settings <file>` | hooks via `hooks.json` / `[hooks]` in `config.toml` |
| hook events | SessionStart, UserPromptSubmit, Pre/PostToolUse, Stop, Notification | SessionStart, UserPromptSubmit, Pre/PostToolUse, Stop (**no Notification**) |
| hook payload | `session_id`, `transcript_path`, `cwd`, `prompt`, `tool_name`, `last_assistant_message` | **same field names** |
| structured transcript | `~/.claude/projects/<munged-cwd>/<sid>.jsonl` | `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<sid>.jsonl` |
| resume | `claude --resume <sid>` | `codex resume <sid>` (also `--last`, `--all`) |
| set cwd | `Popen(cwd=…)` | same, or `--cd/-C` |
| per-invocation overrides | `--settings` | `-c/--config key=value` (repeatable), `--profile/-p` |
| MCP | `~/.claude.json` `mcpServers` | `codex mcp add/list`, `config.toml` |
| model choice | `--model` | `--model/-m`, plus `model_reasoning_effort` |
| usage numbers | OAuth usage endpoint | `rate_limits` inside `token_count` events in the rollout JSONL |

The hook payload compatibility is the headline: `on_hook()` in `server.py`
(L1570) keys off `hook_event_name`, `prompt`, `tool_name`, `session_id`,
`transcript_path`, `last_assistant_message` — **Codex emits all of those under
the same names.** The busy pill, the tab-age anchor, `_follow_session`'s
rotation handling, and the naming/digest triggers all work unmodified.

## The seven real gaps

Ranked by how much they cost.

### 1. Terminal scrollback is weak on Codex — the design-forcing problem

Codex renders in the alternate screen by default. There *is* an escape hatch
(`--no-alt-screen`, or `tui.alternate_screen = "never"`), the direct analogue of
our `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1` pin (gotcha #3) — and it is now
passed on every codex spawn. **Verified working**: a live session's PTY stream
contains zero `\x1b[?1049h`, so bytes land in the normal buffer where the ring
replay and seed can reach them.

The remaining problem is the one the flag can't fix: **Codex still does
full-screen in-place redraws** (its stream is dense with absolute cursor moves
like `\x1b[16;2H`) rather than appending — so scrollback does not *accumulate*
even in the normal buffer ([codex#10331](https://github.com/openai/codex/issues/10331),
[#20063](https://github.com/openai/codex/issues/20063)).

Everything the harness built for mobile scroll-up — the ring replay, the
width-change invalidation in `_apply_size`, `_history_seed_bytes` (`SEED_*`) —
assumes an inline renderer. On a codex session, the phone pan finds an empty
buffer, and there's no env var that fixes it.

**Consequence:** for codex sessions the scroll surface has to be the
**transcript view**, not the terminal. That view was pulled (`DEEP_VIEW`), but
the rollout JSONL is fully structured, so reviving it for codex is
straightforward — and it's arguably where `docs/UNIFIED-SESSIONS.md` was headed
anyway. This is the one decision that shapes the whole port; make it first.

### 2. No `--session-id` — the cid binding inverts

Claude lets us *dictate* the session id at spawn (`--session-id`), so the
cid↔sid map exists before the process does. Codex assigns its own id and offers
no preset flag.

Fix: **inject `HARNESS_CID` into the child env** and have the hook command
echo it back (hook processes are children of codex, so they inherit it). The
`/hook` endpoint already routes by `cid` in the query string — the codex hook
command just reads `$HARNESS_CID` instead of having it baked in. The session
learns its `session_id` from the first SessionStart payload, and
`_follow_session` (L1952) already handles id changes because claude rotates ids
on compaction. Small change; the machinery exists.

Fallback glob for `_find_transcript` (L1949) becomes
`$CODEX_HOME/sessions/*/*/*/rollout-*-<sid>.jsonl`.

### 3. Hooks aren't per-session

`--settings <path>` gives every claude session its own throwaway hook file
(`_write_hook_settings`, L1548) that never touches the user's config. Codex
discovers hooks from config layers (`$CODEX_HOME/hooks.json`, `<repo>/.codex/`),
not from a CLI flag.

Options, best first:
- **One `hooks.json` per account `CODEX_HOME`**, with a command that reads
  `$HARNESS_CID` (gap 2). Written once at account setup, not per spawn. Clean,
  and it keeps project dirs untouched.
- `-c hooks.Stop=…` inline overrides at spawn — repeatable `-c` accepts TOML
  values, so a whole hook table can go on the command line. Uglier, but truly
  per-session if that turns out to matter.
- A per-session `CODEX_HOME` symlinking `auth.json`/`config.toml` — most
  isolated, most moving parts. Not recommended.

### 4. Approvals: `PermissionRequest`, not `Notification` — SOLVED

Codex has no `Notification` event, which is how we set `waiting` (the "blocked,
needs you" state) for claude. It turned out to have something better: a
dedicated **`PermissionRequest`** event, verified present in 0.147.0. `on_hook`
normalises it to `Notification` at the door, so one state machine serves both
engines and the blocked pill works unchanged.

Sessions also spawn with `-a never -s danger-full-access` (`CODEX_APPROVAL` /
`CODEX_SANDBOX`) — the equivalent of the bypass-permissions posture claude
sessions already run in, so routine tool use never blocks a turn that no
browser client could unblock.

### 5. `SCRUB_ENV` needs codex names

Gotcha #1 — inherited env putting the child into nested/embedded mode and
billing metered API instead of the subscription — has an exact codex analogue.
`OPENAI_API_KEY` in the child env means codex may authenticate as API (billed
per token) rather than the ChatGPT plan. Add `OPENAI_API_KEY`, `CODEX_*`,
`OPENAI_BASE_URL` to the scrub list for codex spawns, and prefer
`codex login status` to assert the session is on the plan, not a key.

### 6. Send/submit timing is unmeasured

`SEND_SETTLE` / `SEND_SETTLE_MIN` (gotcha #2) are empirical constants for
claude's paste heuristic. Codex's TUI has its own bracketed-paste and
Enter-to-submit handling; the numbers will differ and must be measured, not
guessed. Same for the **key bar** escape sequences — codex's menus, approval
prompts and slash commands (`/status`, `/usage`) are its own.

### 7. The subscription router does not port

This is the big one to *exclude*, not to solve. `EXPECTATIONS.md` and the
~1500 lines behind it — `claudeAiOauth` blobs, the `platform.claude.com` token
endpoint, the `claude-cli/<ver>` UA requirement, keychain reads, the onboarding
seed, the tri-state creds probe, `_route_key`'s weekly-reset policy, bounce
rescue, limit-wall rescue, rebalance — are Anthropic-specific end to end.

Codex's equivalents are weaker:
- **Multi-account** is fine in principle: N `CODEX_HOME` dirs, each with an
  `auth.json` from `codex login`.
- **Usage data is not.** The only machine-readable numbers are `rate_limits`
  (`primary` = ~5h window, `secondary` = weekly; each with `used_percent`,
  `window_minutes`, `resets_in_seconds`) emitted in `token_count` events inside
  the rollout JSONL. That's **pull-from-transcript, not an endpoint**: it only
  updates when a turn runs, it's reported null in some modes
  ([#14880](https://github.com/openai/codex/issues/14880),
  [#14728](https://github.com/openai/codex/issues/14728)), and an idle account's
  numbers go stale with no way to refresh them. You cannot build
  "poll every 3 min, route to the pool whose weekly resets soonest" on that.

**Recommendation: v1 ships codex as single-login.** The accounts panel shows
codex logins read-only. The never-see-a-rate-limit contract in
`EXPECTATIONS.md` stays a claude-only promise, and should say so explicitly
rather than silently under-delivering on codex sessions.

## What was built

An **`Engine`** strategy object, one instance per engine, reached as `s.eng`.
Everything a CLI does *differently* lives behind it; adding a third engine
should mean writing one subclass, not grepping for "claude".

```
class Engine:
    name, bin, routes_accounts, scrub_extra
    argv(s)              # fresh vs resume
    env(s, env)          # config-dir var, alt-screen pin (mutated in place)
    hook_setup(s)        # per-session settings file (claude) / shared hooks.json (codex)
    transcript_globs(s)  # where this engine's transcript lives
    slim_event(s, line)  # transcript line -> our engine-independent event shape
    send_settle(big)     # the paste/submit constant (gotcha #2) is per-TUI
    bg_probe(s)          # claude's background-work file; "" elsewhere
```

`server.py`:

- `ClaudeSession` gains `engine` (ctor param → `self.engine`, `s.eng` resolves
  the object). `start()` is now ~4 lines of engine calls where the claude
  specifics were inline.
- `_find_transcript`, `_slim_event`, `send_message`, `poll_bg` delegate.
  `_slim_event_claude` / `_slim_event_codex` are the two parsers;
  `_history_seed_bytes`, `_backfill_last_answer`, naming, digests and search all
  ride on `_slim_event`, so they became engine-blind for free.
- `create_session(pid, account, ceremony, engine)` short-circuits for engines
  with `routes_accounts = False`: no account, no config dir, no headroom math.
- **Every account-router path is fenced** behind `routes_accounts` —
  `maybe_handoff`, the rebalance sweep, `rescue_bounced_prompt`,
  `rescue_limit_wall`, and both PTY tripwires (`_scan_for_limit`,
  `_scan_for_onboarding`). Those read *claude's* screens and act by moving a
  session between Anthropic subscriptions; on codex they'd be, at best,
  meaningless and at worst a false-positive respawn of a healthy session.
- The registry persists `"engine"`, defaulting to `"claude"` on load, so a
  pre-engine `.clawd-harness.sessions.json` resumes exactly as before. Resume
  for non-claude engines skips the whole credential gate.
- `clone_for_respawn` needed no change — it derives kwargs from the ctor
  signature, so `engine` rides across automatically. `tools/test_respawn_clone.py`
  covers it generically and passes (19 ctor params, 18 persisted fields).
- `on_hook` normalises codex's `PermissionRequest` → `Notification` and is
  otherwise untouched: codex's payloads use the same field names.

`index.html`: a `codex` button beside ＋ new session (deliberately secondary —
claude stays the one-tap default), an `engine` field on the `new` frame, and a
green `codex` badge on session tabs and cards. The badge renders only for
non-claude, so seeing one always means something.

`docs/WS-PROTOCOL.md`: `new` gains `engine`; `sessionMeta` carries it. Fleet
needed no changes — the worker is just a WS client.

## Verification

Run against an **isolated scratch copy** of the harness (port 8899, its own
`CODEX_HOME`, its own registry) — never the live one, which would resume the
real sessions:

```
cp server.py index.html <scratch>/ && cd <scratch>
PORT=8899 CONSOLE_TOKEN=testtok CODEX_HOME=<scratch>/codexhome python3 server.py
# then drive it over the WS protocol: {type:"new", pid, engine:"codex"}
```

What that proved, on 0.147.0:

| check | result |
|---|---|
| `engine` in session meta / registry | `codex` / `codex` |
| child process argv | `codex --no-alt-screen -a never -s danger-full-access` |
| alt-screen escapes in the PTY stream | `\x1b[?1049h` × **0** |
| PTY paints | 78 KB, renders codex's sign-in picker correctly |
| `CODEX_HOME` isolation | codex wrote its stores into the scratch dir |
| `hooks.json` install | written, mode 0600 |
| close | clean; no orphaned children |

The hook events themselves were confirmed present by string-scanning the
0.147.0 binary (`PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `SessionStart`,
`Stop`, `PermissionRequest`, `hook_event_name`, `transcript_path`,
`last_assistant_message`) — that compatibility is the whole reason this port is
cheap. **Whether they fire end-to-end still needs a signed-in codex.**

## The alternative worth knowing about

`codex app-server` — a long-lived JSON-RPC 2.0 process (stdio, or
`--listen ws://…`) with `thread/start`, `turn/start`, and streaming
`item/started` / `item/agentMessage/delta` notifications. It's what the VS Code
extension and the desktop app use. No PTY, no TUI, no alt-screen problem,
structured approvals.

It's the cleaner long-term integration — and if gap 1 pushes codex sessions
onto a rendered transcript view anyway, the terminal channel is buying less for
codex than it does for claude. But it's a second, different session model
inside the harness, and v1 gets far more reuse from the PTY path. **Recommend
PTY for v1; revisit app-server if codex sessions become the common case.**

## What's next

- **Phase 1 ✅ shipped** — engine abstraction, codex adapter, single login,
  terminal view, hooks installed, resume, engine picker in the UI.
- **Phase 1.5 — the login-gated checks.** Run `codex login` on the harness box,
  then start one codex session and confirm, in order: (1) the busy pill moves
  (hooks fire and `$HARNESS_CID` survives codex's shell invocation);
  (2) `_slim_event_codex` renders a real rollout — compare a live
  `$CODEX_HOME/sessions/…/rollout-*.jsonl` against the parser's assumed shapes
  and fix what differs; (3) time a send to tune the codex `send_settle`.
  Until (1) passes, a codex session's badges are decorative.
- **Phase 2 — scrollback.** Transcript view revived as codex's scroll surface
  (gap 1). Decide this before leaning on codex from a phone.
- **Phase 3 — multi-account codex,** only if `rate_limits` in the rollout
  proves trustworthy enough to route on. Currently assumed not.

Also unbuilt on purpose: the key bar still sends claude's escape sequences, and
`QUICK_PROMPTS` is claude-shaped. Both are fine for a terminal you're typing in
directly; revisit if codex becomes a daily driver.

## Footnote on the motivating complaint

The trigger was verbosity/wrongness from Fable/Opus. A second engine is the
right answer to "I want a genuinely different model's judgment" — but a cheaper
lever remains unbuilt and is worth an afternoon: **per-session model choice**
(`--model`) surfaced in the UI, for both engines. Codex's
`model_reasoning_effort` (`none|minimal|low|medium|high|xhigh|max`, confirmed in
the binary) and `--profile` layering are the model for how to expose it.

## Sources

- [Codex advanced configuration (hooks, notify, CODEX_HOME, profiles, `-c`)](https://learn.chatgpt.com/docs/config-file/config-advanced)
- [Codex developer commands / CLI flags](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
- [Codex hooks: events + payload schemas](https://codex.danielvaughan.com/2026/04/15/codex-cli-hooks-complete-guide-events-policy-patterns/)
- [Session/rollout files](https://github.com/openai/codex/discussions/3827)
- [`--no-alt-screen` still has no usable scrollback (#10331, #20063)](https://github.com/openai/codex/issues/10331)
- [`rate_limits` null in rollout files (#14880)](https://github.com/openai/codex/issues/14880)
- [Codex app-server protocol](https://developers.openai.com/codex/app-server)
