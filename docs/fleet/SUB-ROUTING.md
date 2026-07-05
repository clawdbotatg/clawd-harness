# Subscription routing across the fleet — PLAN (not built)

**Status: plan only — PARKED.** Nothing here is implemented, and as of
2026-07-05 the whole idea may be moot (the second subscription might go
away). This doc exists so the design isn't lost if it comes back. Written
after studying [claw-router](https://github.com/dennisonbertram/claw-router)
(local clone at `projects/claw-router` — gitignored and disposable; the
GitHub repo is the durable reference).

**If only part of this ever gets built, build Phases 0–1.** The
always-logged-in account layer has standalone value even with a single
subscription in rotation (instant manual switching via `cr use`, usage
meters via `cr usage`), and it's the only part with a one-time ceremony
cost. Everything from Phase 2 on is ordinary code that can be written
whenever.

## Goal

Two Claude subscriptions, 3–4 machines, each running a harness with N
interactive `claude` sessions. We want:

- every machine **permanently logged into both** subscriptions;
- one **fleet-wide "active subscription"** that new sessions spawn under;
- the fleet **switches** to the other subscription when it has meaningfully
  more headroom — **debounced by hours**, with hysteresis, so it never
  ping-pongs;
- (later) idle sessions migrate to the active subscription mid-conversation
  without losing their transcript.

## The mechanism (what claw-router proved)

1. **Isolated logins are one env var.** Claude Code keys its credential store
   off `CLAUDE_CONFIG_DIR`: each distinct dir gets its own Keychain item
   (macOS: `Claude Code-credentials-<sha256(dir)[0:8]>`) or its own
   `<dir>/.credentials.json` (Linux). One `claude /login` per (machine ×
   account) dir, once — Claude Code refreshes its own tokens after that.
2. **Headroom is readable.** `GET https://api.anthropic.com/api/oauth/usage`
   with `Authorization: Bearer <accessToken>` +
   `anthropic-beta: oauth-2025-04-20` returns per-window utilization
   (`five_hour`, `seven_day`, `seven_day_opus`, `seven_day_sonnet`, each with
   `utilization` 0–100 and `resets_at`). Headroom = 100 − max(windows).
   On 401, refresh via `platform.claude.com/v1/oauth/token` with Claude
   Code's public client id. **Undocumented — always degrade gracefully.**
3. **Sessions survive an account switch.** A transcript symlinked from its
   owner's `<config-dir>/projects/<munged-cwd>/<sid>.jsonl` into the other
   account's dir makes `--resume <sid>` work under either account.

## Division of labor

**Use claw-router as installed infrastructure** on every machine (the login
ceremony, config-dir layout, settings-sharing symlinks, usage poller).
**Build the switching brain ourselves** in the relay — `cr` decides
per-launch per-machine with no cross-machine debounce, and its `--watch`
supervisor would fight the harness for ownership of the claude child.
Usage is per-*subscription* (all machines burn the same windows), so the
decision must have exactly one owner: the relay.

## Phases

### Phase 0 — validate by hand (no code)

On one machine: install `cr`, `cr register-default`, `cr add <second-sub>`,
`cr policy usage-aware`, `cr usage`. Confirms the endpoint works, both logins
coexist, and shows the real headroom bars. If the endpoint is dead or gated,
stop here and rethink.

### Phase 1 — account layer on every machine (cr as infra)

- Install claw-router on each box (`docs/fleet/ADD-MACHINE.md` gets a step).
- `cr register-default` (adopt the existing login as sub A), `cr add sub-b`.
  Account dirs live at `~/.claw-router/accounts/<name>`; the name→dir map is
  `~/.claw-router/config.json` — the harness will read it, never write it.
- `cr` symlinks `settings.json`/`CLAUDE.md`/`skills/agents/hooks/plugins`
  from `~/.claude` into each account dir, so both accounts have an identical
  environment. Run `cr relink --all` after any account is added.
- macOS: first background poll prompts per-account Keychain access — click
  **Always Allow** once per account. Linux box (`ubuntu@174.129.67.164`):
  credentials are plain `.credentials.json`, no ceremony.

### Phase 2 — harness spawns under an account

`server.py` changes:

- An `ACTIVE_ACCOUNT` (name) held by the manager, persisted in the registry,
  defaulting to `default` (i.e. plain `~/.claude`, `CLAUDE_CONFIG_DIR`
  unset) so a harness with no accounts configured behaves exactly as today.
- `ClaudeSession` records `account` + resolved `config_dir` **at spawn** and
  persists them — a `--resume` after a harness restart must reuse the same
  dir or the transcript/login won't be found. `_spawn` sets
  `env["CLAUDE_CONFIG_DIR"]` (our `SCRUB_ENV` at `server.py:163` doesn't
  touch it — keep it that way).
- Fix the two hardcoded transcript globs to use the session's recorded dir
  (fallback `~/.claude`): `_transcript_exists` (`server.py:231`) and the
  tailer glob (`server.py:641`).
- WS: expose `account` in session meta + a `{type:"account", name}` control
  to set `ACTIVE_ACCOUNT` manually. Sync `docs/WS-PROTOCOL.md`.
- Direct mode (no fleet) keeps working: the harness can apply the same
  switch rule locally from its own poll if no relay tells it otherwise.

### Phase 3 — headroom reporting (worker → relay)

- Worker shells out to `cr status --json --refresh` on a jittered ~10–15 min
  timer (cr's own TTL bounds actual network calls) and piggybacks a compact
  `accounts: [{name, usagePct, exhausted, checkedAt}]` onto the existing
  `stats` frame it already pushes (`worker.py report_stats`, relay
  `t == "stats"` handler). No new message type.
- Missing `cr` / failed poll → omit the field; the relay treats absent data
  as "no opinion".

### Phase 4 — the switch rule (relay)

Relay holds `{active_sub, last_switch_at}` (persisted to disk beside the
passkey state). On each stats update, over the **freshest** per-sub numbers
across all workers:

```
switch to OTHER iff
    headroom(OTHER) − headroom(ACTIVE) ≥ HYSTERESIS   (default 20 pts)
AND now − last_switch_at ≥ DEBOUNCE                   (default 2 h)
OR  usage(ACTIVE) ≥ EXHAUSTED                          (default 95% — bypasses debounce;
                                                        no loyalty to a dead account)
```

Stale data (checkedAt older than ~30 min) → no switch. On switch, broadcast
`{type:"account", active}` to every worker → worker forwards to its harness
→ harness flips `ACTIVE_ACCOUNT` for **new spawns only**. Existing sessions
finish on their old account (cheap tier — zero interruption risk, most of
the value). Surface the active sub + per-sub headroom bars on the machines
rung; badge sessions running on the non-active account.

### Phase 5 (later) — mid-session handoff

On a Stop hook, if `session.account != ACTIVE_ACCOUNT` and the session is
idle: symlink its transcript (+ subagents dir) into the active account's dir
(port of `cr_link_session`, ~40 lines — real-file-wins, never clobber),
then respawn via the existing graceful-restart machinery with `--resume` and
the new `CLAUDE_CONFIG_DIR`. This is claw-router's `--watch` handoff, but
easier for us: we have hook-driven busy state instead of transcript-mtime
guessing, and the restart/`--resume` path already exists.

## Risks / open questions

- **Undocumented endpoint.** May change or vanish. Every consumer must
  degrade to "stay put" (never crash, never flap to a blind guess).
- **Poll fan-in.** 4 machines × 2 subs polling is fine at 15 min TTL, but
  stagger with jitter; if rate limits ever appear, designate one poller.
- **Rejected alternative — static partition** (machine 1–2 on sub A, 3–4 on
  sub B): simpler, no coordination, but halves burst capacity everywhere and
  still exhausts one sub while the other idles. The dynamic switch is the
  point.
- **Rejected alternative — `cr` as the launcher inside the harness**: no
  global debounce (machines flap in sync), two supervisors fighting over one
  child, and the harness couldn't find the transcript without parsing cr's
  stderr banner.
- Fleet code-discipline rule holds: the worker only shells `cr` and talks
  the WS protocol; it still never imports `server.py`.
