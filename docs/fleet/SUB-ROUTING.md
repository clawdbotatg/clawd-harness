# Subscription routing — multi-account, usage-aware

> Reading the accounts panel / debugging why a card's graphs look wrong:
> that's [ACCOUNTS-PANEL.md](ACCOUNTS-PANEL.md). This doc is the routing
> machinery behind the numbers.

**Status: Phases 0–2 + the local switch rule are BUILT (in the harness).**
The fleet-wide relay brain (Phases 3–4) and mid-session handoff (Phase 5) are
still plans. Mechanisms were studied from
[claw-router](https://github.com/dennisonbertram/claw-router) (local clone at
`projects/claw-router`, gitignored/disposable) and then **implemented in-house**
— the harness is pure-stdlib Python and already owns the child env, a poll
loop, and a persisted registry, so depending on an external bash+jq CLI (and a
second config store it would own) bought us nothing. Credit where due: the
four load-bearing mechanisms below are claw-router's discoveries.

## Goal

N Claude subscriptions, several machines, each running a harness with many
interactive `claude` sessions. Every machine permanently signed into every
subscription; new sessions spawn under whichever account has the most
headroom; switching is hysteresis'd and hours-debounced so it never
ping-pongs; running sessions are never interrupted.

## The four mechanisms (all in `server.py` now)

1. **Isolated logins are one env var.** Claude Code keys its credential store
   off `CLAUDE_CONFIG_DIR`: each distinct dir gets its own Keychain item
   (macOS: `Claude Code-credentials-<sha256(NFC(dir))[0:8]>`) or its own
   `<dir>/.credentials.json` (Linux). One OAuth login per (machine × account)
   dir, once — Claude Code refreshes its own tokens after that. An account
   dir's **absolute path keys its Keychain item — never move it**
   (`~/.clawd-accounts/<name>`, `CLAWD_ACCOUNTS_DIR` to override).
2. **Headroom is readable.** `GET api.anthropic.com/api/oauth/usage` with
   `Authorization: Bearer <accessToken>` + `anthropic-beta: oauth-2025-04-20`
   returns per-window utilization (`five_hour`, `seven_day`,
   `seven_day_opus`, `seven_day_sonnet`; each `utilization` 0–100 +
   `resets_at`) plus a newer `limits` array carrying model-scoped caps
   (e.g. the Fable weekly limit as `kind:"weekly_scoped"` with
   `scope.model.display_name` and `percent`) that have no legacy key.
   Headroom = 100 − max(windows, scoped limits). On 401, refresh via
   `platform.claude.com/v1/oauth/token` with Claude Code's public client id.
   **Undocumented — every consumer degrades to "stay put" (keep the last
   snapshot, never flap to a blind guess).** `tools/usage_probe.py` validates
   the whole chain standalone.
3. **Settings sharing.** `settings.json` / `CLAUDE.md` / `skills` / `agents` /
   `hooks` / etc. are symlinked from `~/.claude` into each account dir, and
   `mcpServers` from `~/.claude.json` are merged into the account's
   `.claude.json` once after sign-in — so every account runs an identical
   environment.
4. **Sessions survive an account switch.** A transcript symlinked into the
   other account's `<dir>/projects/<munged-cwd>/` makes `--resume <sid>` work
   under either account. (Not yet used — reserved for Phase 5.)

## What's built (direct mode / per-harness)

- **Accounts registry** in `.clawd-harness.sessions.json` (`accounts`,
  `active_account`, `last_switch_at`). `default` = the machine's plain
  `~/.claude` login, always present, empty config dir → sessions spawn
  exactly as before accounts existed.
- **Sign-in ceremony in the harness UI**: the accounts panel (foot of the
  projects rung) → name an account → `{type:"accountAdd"}` → the harness
  creates the dir + symlinks and spawns a **normal claude session** in the
  self project with `CLAUDE_CONFIG_DIR` pointed at it; the browser is
  deep-linked into its terminal, where you complete the OAuth login. The
  poller notices credentials appear (~15s) and flips the account to ready —
  no CLI, no SSH. (Do it from a browser on the machine itself: claude opens
  the OAuth page in that machine's browser.)
- **Sessions record their account at spawn** (`account` + resolved
  `config_dir`, persisted) — a `--resume` after a harness restart reuses the
  same dir, and the transcript tailer globs under it.
- **Usage polling**: every ~15s tick the poller checks pending accounts for
  first credentials and refreshes ready accounts' usage every `USAGE_TTL`
  (600s default); snapshots persist so bars render instantly after a restart.
  `{type:"accountsRefresh"}` forces a poll.
- **Local switch rule** (`SUB_AUTOSWITCH=1` default): switch the ACTIVE
  account to the one with the most headroom iff it wins by
  `SUB_HYSTERESIS` (20 pts) AND the last switch was `SUB_DEBOUNCE` (2h) ago —
  OR the active account is ≥ `SUB_EXHAUSTED` (95%) used, which bypasses the
  debounce (no loyalty to a dead account). Only ever affects NEW spawns;
  running sessions finish on their old account. Manual override:
  `{type:"accountUse", name}` or the panel's `use` button.
- Wire contract: the `accounts` frame + controls in `docs/WS-PROTOCOL.md`.

## Still planned

### Phase 3 — headroom reporting (worker → relay)

Worker piggybacks each harness's `accounts` snapshot (it already receives the
broadcast frame) onto the existing `stats` frame it pushes to the relay —
`accounts: [{name, usagePct, exhausted, checkedAt}]`. Missing data → omit the
field; the relay treats absence as "no opinion".

### Phase 4 — the switch brain moves to the relay

Usage is per-*subscription* (all machines burn the same windows), so the
decision must have exactly one owner or machines flail independently — each
harness's local rule has its own debounce clock. The relay holds
`{active_sub, last_switch_at}` (persisted beside the passkey state), applies
the same rule over the freshest per-sub numbers across all workers (stale
> ~30 min → no switch), and on switch broadcasts an `accountUse` down every
worker's harness WS. Harnesses under a relay should run with
`SUB_AUTOSWITCH=0` so the relay is the only decider. Surface per-sub headroom
bars + the active sub on the machines rung; badge sessions on the non-active
account (the session `account` field already flows).

### Phase 5 — mid-session handoff (BUILT)

`maybe_handoff` runs after every Stop: if the session's plan is drained
(≥ `SUB_EXHAUSTED` used — checked with a LIVE usage fetch, the 10-min poll is
too slow for a dying window — or its login broke) and a better plan is ready,
the session is respawned under it with `--resume`: transcript (+ subagents
dir) symlinked into the target config dir (never clobbering), the session
object replaced under the SAME cid, viewers re-subscribed. A poller-driven
`_handoff_sweep` catches sessions idling on a drained plan that never emit
another Stop (their last turn died on the limit screen). Per-session
`HANDOFF_COOLDOWN` (600s) stops churn. This is the piece that makes the
guarantee hold for LONG-LIVED sessions, not just fresh spawns: no session
ever sits on a dead plan while another has headroom.

## Risks / notes

- **Undocumented endpoint** — may change or vanish. Everything degrades to
  "stay put"; if it breaks, check the claw-router repo for the fix first.
- **Token refresh**: our 401→refresh path uses the account's refresh token
  but never writes the result back — Claude Code refreshes and persists its
  own tokens; we only borrow read access. (Same behavior as claw-router.)
- **macOS Keychain**: the first `security find-generic-password` per account
  item may prompt — click **Always Allow** once per account on each Mac.
  Linux needs nothing (`.credentials.json`).
- **Rejected: `cr` as installed infra** — external code executing with
  Keychain access on every machine, a second config store, bash+jq deps, and
  its per-launch policy has no cross-machine debounce. We ported the ~200
  load-bearing lines instead.
- **Rejected: static partition** (machine per sub): halves burst capacity
  everywhere and still exhausts one sub while the other idles.
- Fleet code-discipline rule holds: the worker only forwards WS frames; it
  never imports `server.py`.
