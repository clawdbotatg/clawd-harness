# Harness modularization and test-infrastructure plan

**Status:** proposed · **written:** 2026-08-07

## Outcome

Reduce the two implementation monoliths (`server.py` and `index.html`) into
well-defined modules without changing the harness protocol, persisted state,
fleet boundary, subscription-routing contract, or live operating behavior.

At completion:

- `server.py` is a thin composition root and executable entry point.
- engine, account-routing, persistence, protocol, session, project, and HTTP
  behavior live in independently testable Python modules;
- `index.html` remains directly servable with no build step, but loads ordinary
  local ES modules for state, protocol, navigation, terminal, and rendering;
- one conventional command discovers and runs the suite, and CI runs it on every
  change;
- the root documentation accurately describes the current harness, fleet,
  controller, and Claude/Codex support.

This is a behavior-preserving refactor. New product features, protocol redesign,
framework adoption, and changes to the routing policy are out of scope.

## Non-negotiable invariants

Every phase must preserve these boundaries:

1. `fleet/` remains a client of the documented WebSocket protocol and never
   imports harness internals.
2. `controller/` remains an independent client and never imports `server.py` or
   the new harness implementation modules.
3. Existing WebSocket and HTTP message shapes remain compatible. Add contract
   tests before moving their implementations.
4. Registry, token, prompt-log, hooks, account, and transcript formats remain
   readable without migration.
5. The promises in `GOAL.md` and `EXPECTATIONS.md` remain the release gate for
   Claude sessions. Account routing and handoff logic must move mechanically
   before it is redesigned.
6. Codex remains fenced from Claude subscription routing through
   `Engine.routes_accounts`.
7. Python stays stdlib-only at runtime. Development-only test tooling is allowed.
8. The browser remains buildless and directly servable by both the local harness
   and fleet relay.
9. UI-only changes retain live reload; Python boot-file changes retain graceful
   idle-aware restart.
10. Never mix extraction with behavior changes in the same commit.

## Target layout

```text
clawd-harness/
  server.py                    # configuration + assembly + main()
  harness/
    __init__.py
    config.py                  # env loading and typed runtime settings
    models.py                  # Account, Project and shared value objects
    persistence.py             # registry and atomic local-state storage
    protocol.py                # WS framing and public frame validation
    engines/
      base.py                  # Engine contract
      claude.py
      codex.py
    accounts/
      credentials.py           # keychain/OAuth reads and refresh
      usage.py                 # polling and normalized usage data
      routing.py               # selection policy and routing decisions
    sessions.py                # PTY lifecycle, transcript following, hooks
    manager.py                 # projects/sessions orchestration
    naming.py                  # title, digest and emoji generation
    http.py                    # HTTP/WS handler and client fan-out
    lifecycle.py               # watch/reload/restart/update loops
  web/
    app.js                     # browser entry point
    state.js
    protocol.js
    navigation.js
    terminal.js
    transcript.js
    projects.js
    accounts.js
    fleet.js
    styles.css
  index.html                   # markup, asset tags, minimal bootstrap only
  tests/
    unit/
    contract/
    integration/
```

The exact filenames can change when code seams show a better boundary. Dependency
direction may not: low-level modules must not import `manager`, `http`, or
`server`.

## Phase 0 — establish a trustworthy baseline

Before moving code:

- Record `python3 --version`, supported macOS/Linux environments, current CLI
  versions, and the exact smoke commands in a checked-in test matrix.
- Add a `tests/run.py` stdlib runner that invokes the existing executable test
  modules and preserves their exit codes. Do not rewrite them all up front.
- Add `pyproject.toml` test configuration only if pytest is deliberately adopted;
  an empty `pytest` or `unittest discover` run must fail, not report success.
- Classify tests as unit, contract, integration, live-CLI, or deployed-fleet.
  Live/deployed checks must be opt-in and clearly named.
- Capture golden fixtures for representative Claude transcript lines, Codex
  rollout lines, hook payloads, registry JSON, and WS frames. Scrub all tokens,
  paths, prompts, and account identifiers.
- Add HTTP/WS contract tests around `list`, `subscribe`, `send`, binary PTY data,
  hooks, upload, project/session mutations, and fleet tunnelling.
- Add a CI workflow that runs syntax compilation, the hermetic suite, secret
  scanning, and an assertion that at least one test was collected.

**Exit gate:** one documented command runs all hermetic tests from the repository
root; an empty suite is a hard failure; the current behavior has fixtures and
contract coverage sufficient to detect accidental drift.

## Phase 1 — extract leaf utilities

Move code with few stateful dependencies first:

1. WebSocket framing helpers into `harness/protocol.py`.
2. naming/digest/emoji helpers into `harness/naming.py`.
3. atomic registry reads/writes and normalization into
   `harness/persistence.py`.
4. environment parsing into `harness/config.py`, represented by an immutable
   settings object passed explicitly to runtime components.
5. small value objects into `harness/models.py` where doing so does not create
   circular imports.

Keep compatibility imports in `server.py` temporarily if tests or tools patch
symbols there. Add focused unit tests for every extracted seam.

**Exit gate:** behavior and public protocol fixtures are unchanged; `server.py`
is smaller; no new module imports `server.py`.

## Phase 2 — isolate engines and transcript normalization

- Move `Engine`, `ClaudeEngine`, and `CodexEngine` into `harness/engines/`.
- Define the engine interface explicitly: argv, environment, hook setup,
  transcript discovery, event normalization, send timing, background probe,
  and account-routing capability.
- Convert Claude transcript and Codex rollout samples into table-driven tests.
- Test injected-context filtering, Responses-API text parts, tool use/results,
  session-id rotation, trust setup, and malformed-line degradation.
- Keep account routing out of engine modules; engines expose capability and
  normalized observations, not routing policy.

**Exit gate:** adding a third engine requires a new subclass/module and tests,
without editing session orchestration except registration.

## Phase 3 — isolate Claude account routing

This is the highest-risk extraction and should happen in three mechanical steps:

1. Move credential/keychain and OAuth refresh behavior to
   `accounts/credentials.py`.
2. Move endpoint polling and normalized usage state to `accounts/usage.py`.
3. Move route selection, hot/exhausted policy, reset ordering, hysteresis, and
   rebalance decisions to `accounts/routing.py`.

Represent routing inputs and decisions as plain values. A decision should say
*what* to do and *why*; the session manager performs the side effect. Put the
reset-soonest, stale-data, same-pool, ceremony, hot evacuation, cooldown, and
fallback cases into deterministic tests using a fake clock.

Do not alter thresholds or rescue behavior during extraction. Compare routing
decisions from old and new implementations against the same captured scenarios
until they are identical, then remove the old path.

**Exit gate:** the two promises in `GOAL.md` are represented by automated policy
tests, and all account-routing code is unreachable for engines where
`routes_accounts` is false.

## Phase 4 — split session lifecycle from orchestration and transport

- Move PTY spawn, input, resize, ring buffer, hook handling, transcript follow,
  status, and shutdown into `harness/sessions.py`.
- Move project discovery/reconciliation and session collection orchestration into
  `harness/manager.py`.
- Inject dependencies for clock, process spawning, filesystem access where
  valuable, usage polling, and event broadcasting. Avoid a generic dependency
  injection framework.
- Move HTTP routes, WebSocket clients, authorization, upload handling, and frame
  dispatch into `harness/http.py`.
- Move file watching, auto-update, UI reload, graceful restart, and resource-limit
  setup into `harness/lifecycle.py`.
- Reduce `server.py` to: load settings, construct components, wire callbacks,
  start lifecycle loops, and serve.

Test process behavior with fake children and temporary PTYs where possible. Keep
one opt-in live Claude test and one opt-in live Codex test for upstream interface
verification.

**Exit gate:** `server.py` contains no domain policy and is preferably under 300
lines; session behavior can be exercised without opening a listening socket;
HTTP behavior can be exercised without launching a real CLI.

## Phase 5 — modularize the buildless browser

First add browser characterization tests for hash routing, session selection,
composer send, transcript rendering, reconnect, fleet wrapping, and account-panel
state. Continue using the existing local Playwright probes for visual geometry.

Then:

- move CSS to `web/styles.css`;
- introduce `web/app.js` as the only entry point;
- extract pure state/protocol/navigation functions before DOM-heavy components;
- extract terminal and transcript rendering next;
- extract projects, accounts, and fleet adapters last because they touch the
  widest state surface;
- serve module assets with correct MIME types and `Cache-Control: no-store` from
  both harness and relay;
- retain `index.html` as the one shared UI source for direct and fleet modes.

Use native ES modules and browser APIs. Do not introduce bundling, Node runtime
dependencies, or a client framework as part of this refactor.

**Exit gate:** `index.html` is mostly markup and asset references; no extracted
module depends on ambient globals except through one documented bootstrap object;
direct and fleet UI smoke tests both pass.

## Phase 6 — documentation and operational hardening

- Rewrite the root README to describe the current four pieces: harness UI,
  Claude/Codex engines, fleet, and controller. Remove completed roadmap items.
- Add a short architecture index pointing to `GOAL.md`, `EXPECTATIONS.md`, WS
  protocol, fleet architecture, controller design, and Codex engine notes.
- Document the module dependency rules and ownership of each persisted format.
- Add a compatibility table for CLI versions and upstream undocumented surfaces.
- Add an operator test command, a contributor test command, and opt-in live and
  deployed verification commands.
- Add a security model covering bearer-token scope, LAN exposure, relay auth,
  bypassed CLI permissions, uploads, local projects, and secret-bearing runtime
  files.
- Add a lightweight release checklist that includes direct UI, fleet UI, Claude,
  Codex, routing, resume, restart, and controller compatibility.

**Exit gate:** a new contributor can identify the correct component, run the
right test tier, and understand the security boundary without reading the two
monoliths.

## Commit and rollout strategy

- Make one extraction per commit and keep commits mechanically reviewable.
- Run the hermetic suite after every commit; run direct/fleet UI smoke checks
  after protocol or browser changes.
- Do not combine the current uncommitted Codex transcript filtering work with
  this refactor. Land or stash it independently before beginning Phase 1.
- Deploy Python extractions through the existing graceful restart mechanism.
  Observe one full usage-poll and at least one session resume before proceeding.
- Deploy routing extraction alone and observe fresh `checkedAt` values, route
  selection reasons, a normal Stop, and a controlled handoff scenario.
- Keep compatibility re-exports for one phase, then delete them only after all
  internal and tool references have moved.
- Roll back by commit, not by selectively copying old functions into new files.

## Definition of done

- All hermetic tests pass through one discoverable root command and CI.
- Direct harness and fleet relay serve the same modular UI successfully.
- Claude and Codex can spawn, send, signal Stop, render transcript events, resume,
  and close.
- Persisted registries created before the refactor load unchanged.
- Controller and fleet contract suites pass without importing harness internals.
- Routing tests encode the `GOAL.md` promises and production usage remains fresh.
- `server.py` and `index.html` are composition/bootstrap files rather than domain
  implementations.
- Documentation and security guidance match the deployed system.

## Recommended first milestone

Complete Phase 0 and extract only WebSocket framing plus transcript parsers. This
creates immediate test value, validates the module conventions, and touches
neither routing policy nor process lifecycle. Reassess the target layout after
that milestone before committing to the remaining filenames.

---

## Review notes — do / don't (living section)

*Added 2026-08-07 after a first review of this plan against the repo. Append new
judgments here rather than silently rewriting the phases above; this section is
the running record of what we actually decided.*

### The honest framing

The monolith is not the problem — the **untested routing policy** is. Every
production incident in memory (mis-routing on stale usage, the 07-11 limit-wall
pileup, the fake-exhausted 429) was a routing/policy bug, not a
code-organization bug. So sequence for **test coverage first** and let the
module layout be a side effect. Phases 0 and 3 are the payoff; the rest is
tidiness that must earn its keep.

### Do

- **Do Phase 0 now, on `main`.** It is purely additive (no runtime code
  touched), so the fleet auto-pull deploying it continuously is harmless. Treat
  Phase 0 as the real deliverable: even if no extraction ever happens, the
  fixtures + contract tests + CI pay for themselves.
- **Source golden fixtures from production captures, not synthetic data** —
  real 429 bodies, stale `checkedAt` states, the dup-org login shape, actual
  transcript/rollout lines (scrubbed). Every nasty routing edge case came from
  live weirdness; synthetic fixtures would encode our assumptions, which is
  exactly what we can't trust.
- **Do all extraction work on a branch, merge at phase boundaries.** Every push
  to `main` is a live deploy: fleet machines auto-pull ~5 min. A multi-commit
  extraction sequence on `main` deploys prod piecemeal, continuously,
  mid-phase. Branches are inert (auto-pull is main-only, ff-only), so merge
  only whole phases.
- **Expand `RESTART_FILES` / `WATCH_FILES` inside Phase 1, not later.** They
  watch `server.py` and `index.html` by name. The first commit that moves code
  into `harness/*.py` silently breaks graceful restart for that code (and
  `web/*` would break live reload). This is invariant 9's failure mode and it
  fires on commit one unless the watch lists move with the code.
- **Stop after the first milestone and judge.** If it felt mechanical and the
  tests caught drift, continue to Phases 2–3 (routing extraction is where the
  `GOAL.md` promises become automated tests instead of prod incidents). If it
  felt like churn, keep the test suite and walk away — nothing lost.
- **Before Phase 1: land or delete stray uncommitted work** (at review time:
  `tools/escprobe.mjs`), per this plan's own precondition.

### Don't

- **Don't schedule Phase 5 (browser split).** Highest blast radius, lowest
  value: `index.html` is live-edited by parallel sessions, the Playwright
  probes assume its current shape, and the split requires `fleet/relay.py`
  changes (serving `web/*` with correct MIME + `no-store`, plus the `__FLEET__`
  injection point) landing in lockstep with a manual pull on the relay box —
  the one machine that does *not* auto-pull. It would ship broken on
  `h.atg.link` while looking fine locally. Revisit only after Phases 0–4 have
  settled in production.
- **Don't touch routing thresholds or rescue behavior during any extraction**
  (the plan already says this — it bears repeating as the #1 way this refactor
  could cause a real outage).
- **Don't let the refactor block normal shipping.** Small fixes keep landing on
  `main` as usual; the extraction branch rebases onto them. If the branch
  diverges painfully, that's a signal the phase is too big — split it, don't
  freeze the repo.

### Decision log

- **2026-08-07** — plan reviewed, verdict: green-light Phase 0 + first
  milestone (WS framing + transcript parsers) on a branch, watch-list fix
  folded into Phase 1, Phase 5 unscheduled. Not yet started.
