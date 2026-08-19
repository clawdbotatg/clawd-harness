# On-Demand Subscription Routing Plan

Implementation handoff for the builder worker.

Status: approved design; not implemented.

## Scope

This plan changes `clawd-harness` only. The related auditor design is the
[clawd-containers worker-pool plan](https://github.com/clawdbotatg/clawd-containers/blob/main/AUDIT-WORKER-POOL-PLAN.md).

The goal is not to make subscriptions last longer. The goal is to spend them on useful
work instead of restarting idle Claude sessions and re-ingesting their full contexts.

## Decisions

1. Keep all configured subscription pools available.
2. Route a session immediately before it needs model work.
3. Prefer the capable pool with the most real headroom.
4. Never move an idle session just to improve its future placement.
5. Preserve immediate rescue for a prompt or turn that hits a real limit wall.
6. Treat organization UUID as the pool identity. Account-directory names are not
   independent capacity.
7. Keep Auto-TLDR unchanged. It is useful, intentional work.

## Current waste

`poll_accounts_loop()` runs about every 15 seconds and calls `_handoff_sweep()`.
The sweep may move idle sessions because their current account is drained, hot,
incapable, or loses the reset-time rebalance rule.

`SUB_HANDOFF_BATCH=2` limits each sweep, but later sweeps keep moving the remaining
sessions. It converts one herd into a short queue; it does not remove the work.

Every `_handoff()`:

1. links the transcript and subagent directory into another account;
2. kills the current Claude process;
3. starts `claude --resume` under the target account;
4. makes Claude reload that session's existing context.

The existing regression test records the observed failure: ten sessions moved in
batches, and logs showed 89 moves in one direction and 67 back. Those resumed sessions
burned capacity even when no user was waiting for them.

## Target behavior

```text
usage poll
  -> refresh account metadata only
  -> do not respawn sessions

prompt arrives for session X
  -> choose the best usable pool for the requested model
  -> keep X where it is, or hand off only X
  -> wait until the resumed Claude process is ready
  -> deliver the prompt exactly once

active turn hits a real limit wall
  -> preserve the existing immediate rescue
  -> resume on the best usable pool
  -> redeliver the eaten prompt or send "continue" once
```

No account poll should cause model inference by itself.

## Pool selection

Add one pool-selection function used by new sessions, prompt preflight, and rescue.
It must compare unique organization pools, not account labels.

Selection order:

1. Exclude accounts that are signed out or broken.
2. Prefer accounts capable of the requested model.
3. Collapse accounts with the same organization UUID into one capacity pool.
4. Prefer a cool pool over a hot or exhausted pool.
5. Among usable pools, prefer the lowest utilization: the most remaining headroom.
6. Use weekly reset time only as a tie-breaker.
7. If usage is stale or unavailable, keep the current usable pool. Do not move on a
   blind guess.

Movement rules:

- If the current pool is dead, exhausted, or cannot run the requested model, move to
  any better usable pool.
- If the current pool is hot and a cool pool exists, move before sending the prompt.
- If both pools are healthy, move only when the target has at least
  `SUB_HYSTERESIS` more headroom. This prevents prompt-by-prompt ping-pong.
- Never move between two account directories in the same organization.
This intentionally changes the existing `_route_key()` policy. Today it prefers the
soonest weekly reset and uses headroom as a tie-breaker. The target policy prefers
headroom and uses reset time as the tie-breaker.

## Prompt-time preflight

Create a manager-owned operation such as:

```python
send_prompt(cid, text, via="", requested_model=None) -> PromptResult
```

It owns routing and delivery as one operation:

1. Resolve the current session object from `cid`.
2. Acquire a per-session routing lock.
3. Re-resolve the session because another rescue may have replaced it.
4. Determine the requested model or engine capability.
5. Select the best usable organization pool.
6. If no handoff is needed, deliver normally.
7. If a handoff is needed, checkpoint current session metadata and call a handoff
   primitive that returns the replacement session.
8. Wait for the replacement's `_started_evt` with a bounded timeout.
9. Confirm the replacement is alive and is still the object registered for `cid`.
10. Deliver the prompt once.
11. Start the existing send watchdog.

The WebSocket `send` branch should call this operation instead of calling
`s.send_message()` directly.

Do not put routing inside raw `write()` or control sends. Keystrokes, slash commands,
login ceremonies, and terminal resize events must not trigger account changes.

## Auto-TLDR

Keep Auto-TLDR's trigger, prompt, thresholds, and user-visible behavior unchanged.

Auto-TLDR is a real model prompt. Send it through the same prompt preflight so it does
not bounce on an exhausted account. Do not create any extra summarization turn beyond
the existing one.

The preflight refactor must preserve:

- one Auto-TLDR at most per eligible human prompt;
- no TLDR-of-a-TLDR loop;
- the viewer/busy/waiting cancellation checks;
- prompt logging and `via="auto"`;
- the existing bounce watchdog.

## Handoff primitive

Refactor `_handoff()` to return an explicit result:

```text
stayed(current_session, reason)
moved(replacement_session, source_pool, target_pool)
failed(current_session, reason)
```

Required properties:

- same stable `cid`;
- transcript and subagent links are never overwritten;
- session registry replacement is atomic under the manager lock;
- viewers follow the replacement;
- prompt metadata and Auto-TLDR state survive replacement;
- only the old process is killed;
- failure leaves the old session usable when possible;
- exactly one caller owns prompt delivery after a move.

The per-session routing lock must serialize prompt preflight, bounce rescue, limit-wall
rescue, and onboarding rescue. Two simultaneous sends to one session must not create
two replacement processes or deliver either prompt twice.

## Remove eager movement

Change `_handoff_sweep()` so normal polling never moves an idle session for:

- reset-time rebalance;
- hot-pool evacuation;
- capability evacuation;
- drained-pool evacuation.

The poller should refresh metadata, update the default account for new work, broadcast
account state, and stop there.

`SUB_HANDOFF_BATCH` and `SUB_CAP_EVAC_BATCH` become unnecessary after all eager paths
are removed. Delete them after the new tests pass; do not leave dormant policy that a
future refactor can accidentally re-enable.

Also remove post-Stop proactive movement from `maybe_handoff()`. A completed turn does
not prove another turn is coming. The next prompt preflight is the correct boundary.

## Keep active-work rescue

Preserve these fallbacks:

- `rescue_bounced_prompt()` for a prompt rejected before hooks begin;
- `rescue_limit_wall()` for a limit banner during a live turn;
- send-watchdog detection;
- prompt redelivery when the original prompt was eaten;
- `continue` when a turn was cut mid-flight;
- onboarding repair, which is not subscription rebalancing.

Rescue must use the same pool selector and per-session routing lock as preflight.
Endpoint 429 by itself remains ambiguous. It may justify movement only with independent
evidence such as a bounced prompt or a real limit banner.

After prompt preflight is stable, the rescue paths should become rare backstops rather
than the normal routing mechanism.

## Background work

A handoff kills the old Claude process and can kill live background agents or shells.

- If the current pool is usable and background work exists, stay and deliver there.
- If the current pool is unusable and background work exists, do not silently kill it.
  Hold the prompt briefly or return a clear blocked result to the client, then retry
  when the background work is safe to move.
- Emergency rescue for a truly dead turn may reclaim a session only under the existing
  evidence rules.

## State and observability

Add one structured log event for every routing decision:

```json
{
  "event": "prompt_route",
  "cid": "...",
  "source_org": "...",
  "target_org": "...",
  "source_pct": 91.0,
  "target_pct": 34.0,
  "requested_model": "opus",
  "decision": "move",
  "reason": "target has 57 points more headroom",
  "prompt_id": "..."
}
```

Record:

- prompt-time stays, moves, failures, and timeouts;
- source and target organization UUIDs;
- usage freshness and capability state;
- handoff context size when measurable;
- resumed-process startup time;
- whether delivery was original, redelivery, or continuation;
- duplicate-delivery prevention decisions;
- any handoff not caused by a prompt or active rescue.

The last metric should remain zero after rollout.

## Implementation files

- `server.py`
  - make pool ranking headroom-first;
  - add per-session routing locks;
  - add manager-owned prompt preflight and delivery;
  - make `_handoff()` return the replacement session/result;
  - remove eager sweep and post-Stop movement;
  - route Auto-TLDR through prompt preflight;
  - keep active rescue paths.
- `test_handoff_batch.py`
  - replace batch-drain expectations with no-idle-handoff tests, or retire the file
    after equivalent coverage exists.
- `test_on_demand_routing.py`
  - add focused pool-selection, concurrency, and exactly-once tests.
- `test_auto_tldr.py` or the current Auto-TLDR test location
  - pin unchanged behavior across a handoff.
- `docs/fleet/SUB-ROUTING.md`
  - update the built behavior and remove eager-rebalance documentation.
- `EXPECTATIONS.md`
  - state that idle sessions are not respawned for account optimization.

Do not mix this with the auditor worker-pool implementation. They solve related waste
but ship and roll back independently.

## Tests

### Pool selection

1. The capable pool with the most headroom wins.
2. Reset time breaks a headroom tie but does not beat materially greater headroom.
3. Two account directories with one organization UUID are one pool.
4. An incapable pool never wins for its missing model when another capable pool exists.
5. Unknown/stale usage keeps a usable current session in place.
6. Hysteresis prevents small-difference ping-pong.

### Idle behavior

1. Repeated account polls move zero idle sessions.
2. A hot idle session stays parked until prompted.
3. A drained idle session stays parked until prompted.
4. A capability-mismatched idle session stays parked until prompted.
5. Ten parked sessions cause zero Claude respawns and zero context re-ingestion.

### Prompt behavior

1. Prompting one parked session can move only that session.
2. A moved session receives the prompt exactly once after readiness.
3. A handoff timeout does not send into both old and new sessions.
4. Two simultaneous prompts to different sessions may route independently.
5. Two simultaneous prompts to one session create at most one handoff.
6. A same-organization account alias never causes a handoff.
7. Background work prevents an optional handoff.

### Rescue behavior

1. A bounced prompt is confirmed, moved, and redelivered once.
2. A real limit banner resumes the turn on another pool.
3. Quoted limit text does nothing.
4. A bare endpoint 429 does not move an idle session.
5. Onboarding repair still works.

### Auto-TLDR

1. Existing Auto-TLDR tests remain unchanged and pass.
2. An eligible Auto-TLDR routes once if its current pool is unusable.
3. It produces one TLDR prompt, not one before and one after handoff.

## Rollout

1. Add decision logging without changing behavior.
2. Add prompt preflight behind `SUB_ROUTE_ON_PROMPT=1`.
3. Run unit tests and a local fake-session soak test.
4. Enable prompt preflight on one harness while keeping eager sweep disabled there.
5. Confirm prompt delivery, rescue, and Auto-TLDR for at least one usage-window cycle.
6. Enable fleet-wide.
7. Delete eager-movement settings and update `SUB-ROUTING.md`.

Rollback must be one flag change. Keep active limit rescue enabled during every stage.

## Acceptance criteria

- Account polling causes zero session respawns.
- Idle sessions consume zero tokens merely because another pool becomes preferable.
- A prompt uses the capable pool with the most headroom, subject to hysteresis and
  safe background-work rules.
- Only the prompted session moves.
- Prompt delivery is exactly once across handoff races and timeouts.
- Organization aliases never masquerade as extra capacity.
- Active limit-wall rescue still works.
- Auto-TLDR works exactly as before.
- Logs show no unprompted account-optimization handoffs.
- Measured context re-ingestion drops without reducing completed user work.

## Builder checklist

- [ ] Read `CLAUDE.md`, `docs/fleet/SUB-ROUTING.md`, and this plan.
- [ ] Trace every call to `_handoff()`, `maybe_handoff()`, `_handoff_sweep()`, and
      `send_message()`.
- [ ] Add routing decision instrumentation.
- [ ] Write failing on-demand-routing tests.
- [ ] Add the per-session lock and explicit handoff result.
- [ ] Add prompt preflight and exactly-once delivery.
- [ ] Route Auto-TLDR through the same path.
- [ ] Remove eager idle and post-Stop movement.
- [ ] Preserve active rescue and onboarding repair.
- [ ] Update `SUB-ROUTING.md` and `EXPECTATIONS.md`.
- [ ] Run the focused tests, then `python3 tools/shipcheck.py` when implementation is
      ready to deploy.
