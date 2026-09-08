# Council — N models argue, one model decides

> Status: **plan, 2026-09-08.** Nothing built yet. Written from Austin's
> description of the claude↔codex review loop he keeps running by hand.

## The problem

Today the loop is manual: claude writes something, codex finds problems,
claude fixes, codex finds more, repeat. It works, and it is tedious. This
is that loop as a protocol, with a fixed cast and a stop condition.

## The shape

One **goal** (a tweet, a plan, an article, a critique, a build). One
**judge** model that owns the final answer. Two or more **worker** models
that each produce their own answer, review each other's, revise, and repeat
until everyone signs off. Then the judge synthesizes the final from the
signed-off versions plus the whole argument.

```
goal ──▶ workers each draft v1 (deep research, first principles, trust nothing)
             │
   ┌─────────▼──────────────────────────────────┐
   │ round k:                                   │
   │   every worker reviews every other worker  │  verdict = green | red + notes
   │   every worker revises from its reviews    │  → v(k+1)
   │   stop when all verdicts on all latest     │
   │   versions are green, or k == max rounds   │
   └─────────┬──────────────────────────────────┘
             ▼
        judge reads final versions + every review → writes the answer
```

Everyone is on the same team. Green means "I would ship this as-is." Red
means "not yet, and here is exactly why." The judge can also end a round
early ("good enough") or force one more ("worker B's red on A is right").

## Decisions

- **New repo, `clawd-council`.** Used from the harness, not part of it.
  Same relationship as `claude-p-agent`. Other names considered:
  courtroom, moot, quorum. Council fits "same team, one chair."
- **Headless one-shot turns, not PTY sessions.** Every step is `claude -p
  --model X` or `codex exec --model Y`. Both exist and both resume a
  prior session, so a worker keeps its own context across rounds
  (`claude -p --resume`, `codex exec resume <id>`). No terminal parsing.
- **Everything is a file on disk.** A run is a directory. You can watch
  it with `ls`, resume it after a crash, and diff versions. No database.
- **Roles are `engine:model` strings.** `claude:fable-5.1`,
  `claude:sonnet`, `claude:opus`, `codex:gpt-5.6`. Adding an engine =
  one adapter file.
- **Convergence is a strict schema, not vibes.** A review turn must end
  with one JSON line: `{"verdict":"green"|"red","blocking":[...],
  "notes":"..."}`. A reviewer that returns malformed JSON is re-asked
  once, then counted red. Green is only counted for the version it was
  written against; a revision resets every verdict on that worker.
- **Bounded.** Default max 3 rounds. The judge always runs, even on a
  non-converged run, and says so in its answer.
- **Billing stays on subscriptions.** `claude -p` bills the sub (see
  🟦 TLDR notes). `codex exec` bills the codex login. No API keys.

## Run layout

```
runs/<run-id>/
  goal.md                     what Austin asked for, verbatim + config
  config.json                 judge, workers, max_rounds, outcome kind
  workers/<name>/
    session                   engine session id (for resume)
    v1.md v2.md ...           each draft
  reviews/r<k>/<reviewer>-on-<author>.json
  judge/final.md              the answer
  log.jsonl                   every turn: who, prompt hash, verdict, seconds
  status.json                 phase, round, green matrix (the UI reads this)
```

For **build** outcomes each worker gets its own git worktree
(`workers/<name>/tree/`) and its "draft" is a branch plus `v<k>.md`
explaining it. Reviewers get the diff. The judge's synthesis for a build
is a writeup plus a recommended branch, not a merge, in v1.

## Prompts (files in `prompts/`, editable)

- `draft.md` — the goal + "deep research, think from first principles,
  trust nothing, double-check everything, produce the deliverable."
- `review.md` — another worker's version + "critique it as a teammate;
  end with the verdict JSON; red needs concrete blocking items."
- `revise.md` — the reviews you received + "update your version; say
  what you changed and what you rejected and why."
- `judge.md` — all final versions + all reviews + "write the answer;
  credit what you took from whom; list anything still disputed."

The anti-sycophancy rule lives in `review.md`: a reviewer may not flip
red→green unless it names the change that fixed each blocking item.

## Interfaces

**CLI (phase 1):**

```
council run --goal goal.md --judge claude:fable-5.1 \
  --workers claude:sonnet,claude:opus,codex:gpt-5.6 --rounds 3 --kind text
council status <run-id>
council resume <run-id>
```

**Harness (phase 2):** a `#/council` page. Form: goal box, judge picker,
worker pickers (model list from each engine), rounds, kind. A live board
that renders `status.json`: one row per worker, one cell per reviewer,
green/red, current round, judge phase. Tap a cell to read the review. Tap
a worker to read its latest version. The final answer at the top when
done. Progress streams over the existing WS as a new verb; contract goes
in `docs/WS-PROTOCOL.md`.

**PM (phase 3):** a `council` verb so the fleet PM can start one from
Telegram. Verb + MCP description + persona updated together, per
`CLAUDE.md`.

## Phases

1. **Repo + protocol, text outcomes only.** `clawd-council` with
   `council.py` (the loop), `engines/claude.py`, `engines/codex.py`, the
   four prompts, a fake `engines/echo.py` so `test_protocol.py` runs the
   whole loop in under a second with no model. First real run: the
   hard-fork tweet with Fable judging Sonnet, Opus, GPT-5.6.
2. **Harness page.** Spawn a run from the UI, watch the board, read the
   answer. One machine runs the council; pick it the way spawn picks the
   coolest box.
3. **Build outcomes.** Worktrees per worker, diff-based reviews, judge
   recommends a branch.
4. **PM verb.** Then the judge can also be asked to open a harness
   session that implements its own answer.

## Open questions

- **Account routing.** Should each claude worker spawn on the coolest
  sub like harness sessions do, or all on one dir? Phase 1: one dir,
  `CLAUDE_CONFIG_DIR` env, same as claude-p-agent. Router later.
- **Where a run lives.** Any fleet box can run one. Phase 1: the box
  you run the CLI on. Phase 2 needs a home for `runs/` the UI can read.
- **Does the judge get a vote during rounds?** Plan says judge only
  ends or extends rounds. Could also let it review. Start narrow.
- **Same model twice.** Two Opus workers with different seeds may be
  useful for plans. Names are `opus-1`, `opus-2`; nothing else changes.

## Cost, roughly

Three workers, three rounds: 3 drafts + 3×(6 reviews + 3 revisions) +
1 judge = 31 turns. Each is a normal `-p` turn. Fine on subscriptions;
a run is minutes, not seconds, so it must be resumable.
