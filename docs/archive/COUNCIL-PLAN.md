# Council: multi-model deliberation runs

> Status: proposed. This plan extends the controller's existing multi-engine
> pipeline machinery into a parallel, iterative review protocol. It does not
> change the harness's role as the execution substrate.

## Product statement

Council takes one outcome, several worker model configurations, and one judge
model configuration. It asks every worker to produce an independent answer,
has the workers review one another, lets each author revise in response, and
repeats the review loop until the work is ready or a configured limit is hit.
The judge then synthesizes the final deliverable and records how unresolved
disagreements were decided.

The user gets one finished artifact, not a chat log they have to manually ferry
between Claude and Codex. The full debate remains inspectable.

Examples of outcomes:

- a researched tweet or article;
- an implementation plan or architecture decision;
- a critique, recommendation, or threat model;
- a software build or patch (with extra workspace isolation described below).

The important form of agreement is **readiness**, not identical opinions. A
worker can prefer different wording and still vote green. A red vote must name
a concrete blocking defect tied to the goal or acceptance criteria.

## Name and roles

Use **Council** in the UI. Use **deliberation** in code and the wire/data model.

- **Judge** (also called the chair in prompts): owns the final answer, resolves
  disputes, and cannot silently ignore an open blocker.
- **Workers**: independently research and draft, review their peers, revise
  their own work, and vote on readiness.
- **Orchestrator**: deterministic controller code that advances phases,
  persists state, applies limits, and handles failures. It is not another LLM.

“Courtroom” is a useful mental model for rigorous challenge, but Council is a
better product metaphor: the participants are collaborators, and dissent is a
tool for improving the common artifact rather than scoring points.

## Where it belongs

Build the first version **inside this repository, in `controller/`**.

The current architecture already has nearly every primitive Council needs:

- the harness can run visible, interactive Claude and Codex sessions;
- the controller can spawn either engine, send prompts, read structured final
  answers, and react to turn completion;
- the event-sourced task ledger survives restarts;
- existing pipelines pass outputs between agents, reuse a worker's session,
  and have a settle-sweep fallback when a Stop hook is lost;
- the controller already uses `claude-p-agent` as its conversational PM brain.

Council is not another agent runtime. It is a graph-shaped orchestration mode
above those pieces. The existing pipeline is a linear list of at most six
steps; forcing fan-out, barriers, repeat rounds, votes, and arbitration into a
huge generated step list would make recovery and inspection brittle.

Suggested seam:

```text
clawd-harness
├── server.py                         execution substrate; minimal model-pin work
└── controller/
    ├── deliberation.py               state machine and scheduler
    ├── deliberation_prompts.py       versioned protocol prompts/schemas
    ├── artifacts.py                  immutable artifact and review storage
    ├── ledger.py                     deliberation events + folded state
    ├── verbs.py / mcp.py             create/start/read/pause/cancel verbs
    └── chat UI                       Council creation and live run view
```

`claude-p-agent` should remain the small turn runner used by the PM. Its own
README explicitly says it has no orchestration loop. Council sequencing belongs
in the controller, where it can be deterministic, restart-safe, visible in the
fleet, and usable with interactive subscription-backed sessions. If this
protocol later needs non-harness backends, extract `controller/deliberation.py`
and its schemas into a separate `clawd-council` package after the protocol has
been proven. Do not pay the second-repo coordination cost in the MVP.

## User contract

A Council run starts from a concrete contract:

```json
{
  "goal": "Write a tweet explaining the new Ethereum hard fork",
  "deliverable": "tweet",
  "acceptance": [
    "factually accurate as of the stated date",
    "under the selected character limit",
    "clear to a technical but non-core-dev audience",
    "includes source links in the research record"
  ],
  "context": "Optional user notes, files, URLs, or project scope",
  "workers": [
    {"id": "sonnet", "engine": "claude", "model": "<sonnet model id>"},
    {"id": "opus", "engine": "claude", "model": "<opus model id>"},
    {"id": "gpt", "engine": "codex", "model": "<codex model id>",
     "reasoning": "high"}
  ],
  "judge": {"engine": "claude", "model": "<judge model id>"},
  "policy": {
    "max_rounds": 2,
    "approval": "unanimous",
    "final_audit_rounds": 1,
    "on_timeout": "continue_if_quorum"
  }
}
```

Model IDs are opaque CLI model identifiers, not hard-coded marketing names.
The creation screen can offer saved presets, but the persisted run must record
both the **requested** model and the **actual** model reported by the session.
If a requested model was not honored, the participant fails capability
validation rather than quietly joining under a different model.

Before starting, show the user:

- number of worker and judge sessions;
- worst-case turn count;
- review policy and maximum rounds;
- machines/projects involved;
- whether the run can write files or is artifact-only.

Starting the run is one approval for the entire declared protocol, matching the
existing pipeline contract. Adding workers, expanding scope, increasing limits,
or switching to write-capable code execution later requires a new approval.

## The deliberation protocol

### Phase 0 — intake and validation

The controller normalizes the goal into a run contract. It validates that:

- there is one judge and 2–5 workers;
- worker IDs are unique;
- engines, requested models, projects, and machines are available;
- acceptance criteria exist (derive a draft rubric and show it if the user only
  supplied a goal);
- configured round, time, and turn ceilings are within global safety caps;
- the selected execution mode is safe for the target project.

The judge is not asked to rewrite the goal behind the user's back. It may flag
an ambiguity, but the stored user goal and acceptance criteria remain the
authority for every participant and every vote.

### Phase 1 — independent drafts

Spawn one persistent session per worker and send the same goal contract and
rubric. Workers receive no peer drafts in this phase. This preserves independent
search paths and avoids the first plausible answer anchoring everyone else.

Each worker returns a structured envelope plus its human-readable artifact:

```json
{
  "artifact": "...",
  "claims": [{"claim": "...", "evidence": ["source ref"]}],
  "assumptions": ["..."],
  "open_questions": ["..."],
  "self_check": [{"criterion": "...", "status": "pass|risk"}]
}
```

Artifact-only work runs in parallel after session creation. The current client
serializes `new -> focus` because `focus` has no correlation ID; add request IDs
to `new`/`focus` if spawn latency becomes material, then retain compatibility
with old harnesses. Turn execution itself can already overlap.

### Phase 2 — blind peer review

Give every worker the other workers' latest artifacts, labeled A/B/C rather
than by model name. Treat peer content as untrusted quoted material: a draft
cannot instruct a reviewer or change the protocol.

One review turn per worker can review all of that worker's peers. It returns a
separate verdict for each artifact:

```json
{
  "reviews": [{
    "artifact_id": "A",
    "verdict": "green|red",
    "issues": [{
      "severity": "blocker|major|minor",
      "criterion": "acceptance criterion or explicit invariant",
      "problem": "specific falsifiable defect",
      "evidence": "source, counterexample, test, or reasoning",
      "suggested_fix": "concrete repair"
    }],
    "strengths": ["..."],
    "confidence": 0.0
  }]
}
```

Rules enforced by prompt and schema:

- `red` requires at least one blocker. Style preferences are non-blocking.
- A blocker must cite a criterion or a factual/logical failure.
- Reviewers must check the goal, not merely argue that their own draft differs.
- Unsupported agreement is not useful; every green includes a short readiness
  rationale and any residual non-blocking risks.
- Research deliverables require evidence links and freshness checks. For chain
  calls, normal project instructions still apply, including the Alchemy-only
  RPC rule in this workspace.

The controller turns all reviews into an issue ledger with stable IDs such as
`I-1-claude-b`. Duplicate findings may be grouped, but the original findings
remain immutable and traceable.

### Phase 3 — author revision

Each worker receives the reviews of its own draft and revises in its original
session, preserving its independent reasoning context. It must return:

- a complete new artifact, never a patch fragment;
- an issue response for every blocker: `fixed`, `rejected`, or `needs_judge`;
- a brief rationale for rejected feedback;
- updated claims, evidence, assumptions, and self-checks.

Rejecting feedback is allowed. Automatic deference would create false
consensus. A rejected blocker remains open until its reviewer withdraws it or
the judge arbitrates it.

### Phase 4 — readiness vote

Every worker sees all revised peer artifacts and the issue responses relevant
to its prior findings. Each returns green/red per artifact and references issue
IDs. A worker also self-checks its own artifact, but its self-vote does not
cancel a peer's red vote.

Default convergence rule:

```text
ready(worker artifact) = every other live worker voted green
round converged         = every live worker artifact is ready
```

This is consensus on “safe for the judge to use,” not consensus that every
artifact is equally good. Quorum modes can be added, but unanimous green is the
honest default for the workflow being requested.

### Phase 5 — repeat or arbitrate

If every artifact is ready, stop early. Otherwise start another review/revision
round, focused on open blockers and any regressions introduced by revision.

The loop is bounded by all of:

- `max_rounds` (default 2, hard cap 5);
- a per-participant turn ceiling;
- a wall-clock deadline;
- a global cancellation switch;
- subscription/account availability.

If the limit is reached without unanimity, do not call it consensus. Move to
judging with an explicit unresolved-issues packet. The judge must decide each
open issue as `accept`, `reject`, or `out_of_scope`, with a rationale. The final
receipt says that the result used judge arbitration and names the remaining
risks.

### Phase 6 — judge synthesis

Spawn a separate judge session only after the worker phase. Give it:

- the immutable goal and acceptance criteria;
- every worker's latest artifact;
- the claim/evidence manifests;
- the issue ledger and final votes;
- all rejected or unresolved feedback;
- a strict instruction to produce the deliverable, not a summary of the debate.

The judge returns:

```json
{
  "final": "the actual deliverable",
  "criterion_check": [{"criterion": "...", "status": "pass|risk"}],
  "decisions": [{"issue_id": "...", "decision": "...", "rationale": "..."}],
  "sources": ["..."],
  "remaining_risks": ["..."]
}
```

The judge owns wording and synthesis, but it cannot erase the audit trail or
claim unanimous agreement when there was dissent.

### Phase 7 — final audit

The synthesis step can introduce new errors. By default, run one short final
audit: workers inspect only the judge's candidate against the acceptance
criteria and return blockers. If there are none, publish it. If blockers exist,
the judge gets one repair turn with the findings. Repeat only up to
`final_audit_rounds`; after that, publish with an explicit judge override or
park the run for the user, according to policy.

This phase is what closes the current “Claude fixes it, Codex finds another
issue” loop: the artifact being delivered is itself reviewed, not merely the
inputs from which it was synthesized.

## Cost and scaling

For `N` workers, one full worker round uses roughly:

```text
N draft turns (once)
+ N review turns per round
+ N revision turns per round
+ N readiness-vote turns per round
+ 1 judge synthesis
+ N final-audit turns per audit round
+ optional judge repair turns
```

With 3 workers, 2 maximum rounds, and 1 final audit, the worst ordinary case is
25 model turns (3 drafts + 18 loop turns + 1 judge + 3 audit turns), plus one
judge repair if needed.
Combining all peer reviews into one structured turn per reviewer avoids the
`N * (N - 1)` turn explosion while still producing an all-to-all review matrix.

The creation UI should calculate this number before launch. Defaults should be
3 workers, 2 rounds, unanimous approval, and 1 final audit. More debate is not
automatically better; correlated models can reinforce a shared mistake while
burning every plan.

## Persistence and state machine

Add a separate deliberation aggregate rather than overloading pipeline steps.

```text
drafting
  -> reviewing(round)
  -> revising(round)
  -> voting(round)
       -> reviewing(round + 1)  when blockers remain and budget remains
       -> judging               on convergence or limit
  -> final_audit
       -> judge_revising        when new blockers remain
       -> completed | needs_user

Any active phase -> paused | cancelled | failed
```

Use phase barriers: a phase advances only when every expected live participant
has submitted the artifact for that phase, or the timeout policy has produced a
recorded degraded-participant decision. Duplicate Stop hooks and reconnect
replays must be idempotent.

Extend the append-only ledger with events along these lines:

- `deliberation_created`, `deliberation_started`;
- `participant_started`, `participant_failed`;
- `phase_started`, `artifact_submitted`;
- `review_submitted`, `issue_recorded`, `issue_resolved`, `issue_disputed`;
- `vote_submitted`, `round_completed`;
- `judge_started`, `judgment_submitted`, `audit_submitted`;
- `deliberation_paused`, `deliberation_resumed`, `deliberation_completed`,
  `deliberation_cancelled`.

Do not put arbitrarily long articles and review matrices directly in the JSONL
event log. Store immutable artifacts under a controller-owned run directory and
put path, content hash, byte count, schema version, actor, phase, round, and
timestamp in the event. Atomic write-then-rename keeps an artifact from being
observed half-written. The event log remains the replayable source of state;
artifact hashes make corruption or accidental rewriting visible.

## Model selection support

Today the harness selects an engine at session creation, but not a requested
model. Add the smallest possible engine-layer extension:

- `new` accepts `model?` and `reasoning?` and echoes a request ID in `focus`;
- `ClaudeSession` persists `requested_model` and `requested_reasoning` so
  restarts and respawns retain them;
- `ClaudeEngine.argv()` appends `--model <id>`;
- `CodexEngine.argv()` appends `--model <id>` and the supported reasoning
  override;
- reject blank, option-shaped, overlong, or malformed values even though argv
  is not invoked through a shell;
- session metadata exposes requested and observed model separately;
- controller `new_session`, `spawn`, `assign`, and the new Council participant
  spec pass the fields through;
- old harnesses fail Council capability validation instead of silently
  substituting a default.

Do not add arbitrary “extra CLI args” to the wire protocol. Model and reasoning
are narrow declarative capabilities; raw flags would turn the controller into a
remote command-argument surface.

## Artifact mode versus code mode

### MVP: artifact mode

Ship Council first for text outputs, research, plans, and read-only critiques.
Workers may read the target project and use normal tools, but prompts prohibit
workspace mutation. The deliverable lives in the Council artifact store until
the judge finishes. This mode is naturally safe to parallelize.

### Later: code mode

Never let several workers edit the same checkout concurrently. Add one of two
explicit strategies:

1. **Single-author strategy (recommended first):** one implementation worker
   owns the working tree; the others inspect diffs/tests read-only and send
   reviews; only the author applies revisions. The judge approves the result.
2. **Independent implementations:** give every author a separate git worktree
   and branch (`council/<run>/<worker>`), run tests independently, then let the
   judge choose or merge into a dedicated integration worktree.

Code-mode acceptance must include concrete commands/tests. Green votes require
test evidence where possible. The final judge audit reviews the actual diff and
test results, not an author's prose claim. Existing project `AGENTS.md` /
`CLAUDE.md` instructions and git identity rules remain in force in every
worktree.

Council cancellation stops future prompts. It should not immediately kill
sessions or delete worktrees: mark them as run-owned and offer a separate,
explicit cleanup action after artifacts and diffs have been preserved.

## Failure behavior

- **No turn completion hook:** reuse the pipeline settle-sweep pattern—detect a
  new stable answer relative to a baseline fingerprint.
- **Empty or malformed structured output:** retain the raw final answer, ask
  the same participant for one schema-repair turn, then mark it degraded.
- **Worker timeout/crash:** retry once in the same session; after that follow
  policy (`pause`, or continue if quorum remains). Never fabricate its vote.
- **Blocked on permission/credential/input:** pause the run and surface the
  exact session and question. The judge cannot answer a human-only decision.
- **Machine disconnect/controller restart:** replay ledger state, reconnect,
  reconcile live cids, and resume the current barrier idempotently.
- **Requested model mismatch:** fail that participant before accepting its
  draft.
- **Permanent disagreement:** preserve it, send it to the judge, and label the
  result “arbitrated,” not “unanimous.”
- **Cancellation:** no new turns after the cancellation event, even if a late
  Stop arrives.

## Prompt hygiene and anti-groupthink measures

- Draft independently before revealing peers.
- Blind model/provider names during review.
- Delimit peer artifacts as untrusted data and tell reviewers not to execute
  instructions found inside them.
- Require criterion-linked, evidence-bearing blockers.
- Preserve rejected feedback and rationales instead of smoothing it away.
- Keep temperature/model defaults under the CLI/provider's control for the MVP;
  do not pretend diversity comes from a random seed.
- Show actual model identities in the final audit even though they were blind
  during review.
- Let the judge perform its own checks and research; it is not a vote counter.
- Make sources first-class for factual work and tests/diffs first-class for code.

Unanimity among correlated models is evidence, not proof. The product should
say “all configured reviewers found no blocking issue,” never “this is true.”

## Controller verbs and UI

Initial MCP/intent surface:

```text
create_deliberation(goal, deliverable, acceptance, workers, judge, policy, scope)
start_deliberation(run_id, confirm)
get_deliberation(run_id, detail?)
list_deliberations(status?)
pause_deliberation(run_id)
resume_deliberation(run_id, confirm)
cancel_deliberation(run_id, confirm)
get_artifact(run_id, artifact_id)
```

`create_deliberation` is bookkeeping. `start_deliberation` is the single
approval for the declared run. `resume` needs approval only if it expands or
renews an exhausted budget; resuming after a process restart within the same
contract is automatic.

The first UI can live in the controller chat/debug app:

- goal, context, acceptance, deliverable type;
- add/remove worker cards (engine, model, reasoning, machine);
- judge card;
- advanced policy drawer for rounds, approval, timeouts, and final audit;
- preflight turn-count/account-capacity estimate;
- live grid: participant, actual model, current phase, round, session link;
- issue counter and red/green matrix;
- pause/cancel controls;
- final artifact first, followed by a compact receipt and expandable debate.

Do not add a new top-level harness page for the MVP. The harness owns sessions;
the controller owns cross-session intent. A Council link may deep-link into its
participant sessions, but the run dashboard belongs to the controller.

## Delivery receipt

Return the final artifact prominently. Under it, include a compact receipt:

- run ID and goal;
- requested and actual judge/worker models;
- rounds completed and why the loop stopped;
- green/red final matrix;
- whether the judge arbitrated anything;
- unresolved risks;
- citations or test evidence;
- links to the run and participant sessions.

Keep raw prompts, drafts, reviews, and issue history available on demand rather
than dumping them into the normal result.

## Implementation phases

### Phase 1 — model pins and correlated session creation

1. Extend the harness `new` frame and session persistence with requested model
   and reasoning fields.
2. Add per-engine argv generation and validation.
3. Echo request IDs in `focus`; update direct and relay controller clients.
4. Expose requested/actual models in session metadata.
5. Add protocol docs and compatibility tests.

Exit condition: the controller can start three sessions with three explicit
model configurations and prove which model actually answered.

### Phase 2 — durable artifact-only Council core

1. Add the artifact store and deliberation ledger events/fold.
2. Implement creation validation and worst-case turn calculation.
3. Implement independent drafting, parallel phase barriers, Stop handling, and
   settle-sweep recovery.
4. Implement structured peer reviews, issue ledger, revisions, and votes.
5. Implement bounded repeat rounds and non-convergence arbitration packets.
6. Implement judge synthesis and final audit.
7. Add read/start/pause/resume/cancel verbs and MCP schemas.

Exit condition: a mocked 3-worker run automatically goes red, revises, reaches
green, is synthesized, audited, persisted, and completed without a PM turn
deciding the next phase.

### Phase 3 — controller UI and real-model pilot

1. Add the Council creation form and run dashboard.
2. Surface capacity warnings from existing account metadata.
3. Run real pilots on a tweet, a plan, and a factual article.
4. Measure prompt size, round duration, hook loss, model mismatch, and whether
   issue schemas actually reduce vague repeated objections.
5. Tune defaults from those measurements, not intuition.

Exit condition: a user can launch and inspect the example Ethereum-fork tweet
from the browser and receive the final artifact plus receipt.

### Phase 4 — code mode

1. Add single-author/read-only-reviewer mode.
2. Capture git diff, HEAD, dirty state, and test evidence as typed artifacts.
3. Add isolated worktree strategy only after the single-author flow is solid.
4. Add explicit preservation and cleanup flows.

Exit condition: Council can implement a small change, survive a red review,
apply the fix in the author session, and deliver a verified diff without
concurrent writes to one checkout.

### Phase 5 — optional extraction

Only consider a `clawd-council` repo if a second execution backend needs the
protocol. Extract schemas, state machine, and artifact storage behind a small
adapter interface; keep harness/controller integration here. Until then,
monorepo locality is an advantage.

## Test plan

Build the core against `controller/mock_harness.py`; real-model tests are smoke
tests, not the correctness gate.

Required deterministic cases:

- three independent drafts launch before any peer material is sent;
- every worker reviews every other worker exactly once per review phase;
- a red blocker reaches the correct author, is fixed, and becomes green;
- a rejected blocker remains open and reaches judge arbitration;
- unanimous green stops before `max_rounds`;
- permanent red stops at the cap and is never labeled consensus;
- judge output receives a final audit and repair turn;
- malformed JSON gets one repair attempt and raw output is preserved;
- duplicate/late Stop events cannot advance a barrier twice;
- lost Codex hooks advance through stable-answer detection;
- worker death, disconnect, pause, resume, cancellation, and controller restart
  recover without duplicating turns;
- requested/actual model mismatch fails preflight or the participant;
- artifact hashes and replay reconstruct the same current state;
- artifact mode never writes the target project;
- code mode never gives two write-capable sessions the same checkout.

## MVP definition of done

Council MVP is done when a user can:

1. enter a goal, acceptance criteria, three worker model configurations, and a
   judge model configuration;
2. approve the bounded run once;
3. watch independent drafting, all-to-all review, revision, and voting proceed
   without manually moving text between sessions;
4. see the loop stop early on unanimous green or honestly report that it hit a
   limit with unresolved dissent;
5. receive a judge-produced final artifact that itself passed a final audit (or
   is visibly marked as an override);
6. inspect every draft, review, issue decision, model identity, and session;
7. restart the controller mid-run and have it continue exactly once from the
   persisted phase.

That is the smallest version that captures the useful behavior in the current
Claude/Codex back-and-forth without disguising an unbounded token-burning loop
as rigor.

## Codex review of Claude's original plan

**Verdict: good protocol sketch; wrong first architecture. Do not implement it
unchanged.**

### Keep

- The Council name.
- Independent drafts followed by review and revision.
- Persistent worker conversations.
- Immutable versioned artifacts on disk.
- Votes bound to one exact artifact version.
- Editable prompt files and a fake engine for fast protocol tests.
- Git worktrees as a later option for independent code implementations.

### Fix before building

1. **Do not start with a new repo and a second runner.** This controller already
   spawns Claude and Codex, routes Claude accounts, reads final answers, handles
   Stop events, survives missing hooks, persists tasks, and exposes MCP verbs.
   A headless `clawd-council` would duplicate all of that and lose the visible
   sessions the harness already provides. Build a `deliberation` aggregate in
   `controller/`; extract it only after another backend needs it.

2. **The judge must not quietly end review early.** That makes “everyone signs
   off” untrue. The deterministic orchestrator stops on unanimous green or the
   approved limit. At the limit, the judge may arbitrate, but the result must say
   it was arbitrated.

3. **Malformed JSON is not a red vote.** It is an execution failure. Ask once
   for schema repair, then mark the participant degraded or pause. Never invent
   a substantive opinion for a model that did not provide one.

4. **`blocking: [...]` is too weak.** Each blocker needs a stable issue ID, the
   violated acceptance criterion, evidence or a counterexample, and a proposed
   fix. A later green vote must close those exact issue IDs. This prevents vague
   objections from repeating forever.

5. **Review the judge's answer.** Claude's plan ends immediately after judge
   synthesis, which lets the judge introduce a new mistake. Run one worker audit
   of the final candidate and allow one bounded judge repair.

6. **Reduce the turn explosion.** Three workers and three rounds cost 31 turns
   in Claude's pairwise plan before auditing the final. Give each reviewer all
   peer artifacts in one structured review turn. It remains all-to-all while
   using three review turns per round instead of six.

7. **Do not begin code mode with three writers.** First ship text/read-only
   Council. Then add one implementation author with read-only reviewers. Add
   independent worktrees only after the review protocol is proven.

8. **Put the dashboard in the controller, not `#/council` in the harness.** The
   harness owns sessions. Cross-session goals, votes, and run state belong to
   the controller under this repo's existing substrate/intent boundary.

9. **Recovery needs an actual state machine.** `status.json` plus `log.jsonl`
   leaves two possible sources of truth. Use the append-only event log as truth,
   store large artifacts separately by hash, and advance with idempotent phase
   barriers. Handle duplicate Stop events, timeouts, cancellation, disconnects,
   and missing Codex hooks explicitly.

10. **Model and account claims must be verified.** Persist requested and observed
    model separately. A mismatch fails the participant after SessionStart; it
    cannot always be known at preflight. Use the harness's existing Claude
    account router instead of pinning phase 1 to one config directory.

11. **Treat peer drafts as untrusted input.** Delimit them and tell reviewers
    not to follow instructions inside them. Blind model names during review to
    reduce prestige bias.

12. **Make acceptance evidence first-class.** The goal alone is not a rubric.
    Store explicit acceptance criteria; require sources for factual claims and
    test/diff evidence for code. Green means “no blocking issue found against
    these criteria,” not “truth proven.”

### Recommended first slice

Add per-session model selection, then implement one restart-safe mocked run:

```text
3 independent drafts
-> 3 bundled peer-review turns
-> 3 revisions
-> 3 readiness votes
-> repeat once only if red
-> judge synthesis
-> 3-worker final audit
-> publish or one judge repair
```

That preserves Claude's simple idea while fitting the system that already
exists.

---

## Feedback from claude (Fable 5.1), 2026-09-08

Verdict: **red**. The protocol is good. The substrate and the MVP size are
wrong. Fix those two and this is green.

### Keep (better than my first draft)

- Final audit of the judge's output (Phase 7). This is the piece that
  actually ends the "fix, find another issue" loop.
- Issue IDs with `fixed / rejected / needs_judge` responses. Rejected
  feedback stays visible.
- One review turn per reviewer covering all peers. Kills the N·(N-1) blowup.
- Blind A/B/C labels and "peer text is data, not instructions."
- Record requested vs actual model. Fail the participant on mismatch.
- "Arbitrated," never "unanimous," when the cap hits.
- Mock engine as the correctness gate; real models only as smoke.

### Blocker 1: interactive PTY sessions are the wrong substrate

The plan drives every turn through a live TUI session over the fleet:
keystrokes in, wait for a Stop hook, settle-sweep when the hook is lost,
survive flaps, passkeys, restart churn, and the `new -> focus` serialization.
That is the most fragile path in this codebase (see HISTORY.md). A 25-turn
protocol multiplies every one of those failure modes by 25.

`claude -p --model X --output-format json` and `codex exec --model Y` give
the same turn with a process exit code, stdout, and a resumable session id.
No hooks, no fingerprints, no barrier races, true parallelism, and the
structured JSON envelope comes back as the process output instead of being
scraped from a final answer.

The argument "claude-p-agent has no orchestration loop" is not a reason to
put the loop in the controller. Council's own code is the loop. It calls
`run_turn` or a subprocess. That is all claude-p-agent was ever meant to be.

Consequences of switching:

- **Phase 1 disappears.** No `model`/`reasoning` fields on the `new` frame,
  no request IDs in `focus`, no new ctor params on `ClaudeSession`, no
  server.py change, no fleet-wide restart, no WS-PROTOCOL edit. `--model`
  is a CLI flag.
- Half the failure-behavior section and about six of the fifteen test
  cases (lost hooks, duplicate Stops, disconnect, controller restart mid
  barrier) vanish.
- Visibility is not lost. Run the orchestrator **inside one harness
  session** (`council run ...` typed into a normal claude session). Austin
  watches it in the terminal, it rides the session's subscription routing,
  and the TLDR summarizes it. Workers are subprocesses of that session.
- Billing is unchanged: `claude -p` bills the sub of the dir it runs in.

### Blocker 2: the MVP is too big to ship

619 lines, 7 protocol phases, 8 verbs, an event-sourced ledger with
14 event types, artifact hashing, atomic renames, wall-clock deadlines,
quorum modes, pause/resume/cancel, restart-safe barriers. The stated goal
is a tweet.

The run directory is the state. Resume = look at which files exist and
continue from the first missing one. That replaces the ledger, the fold,
the hashes, and the idempotency work with `ls`. Add a ledger later if a
real run ever needs it.

MVP = CLI + prompts + mock engine + one real run. No controller verbs, no
UI, no pause/resume beyond "rerun the command."

### Smaller cuts

- **Drop Phase 4 (separate vote turn).** The next round's review is the
  vote on the revised artifact. All green on a review pass = converged, no
  revise turn needed. Saves N turns per round.
- **Acceptance criteria must be optional.** Nobody writes a rubric for a
  tweet. The drafter derives one from the goal and states it; the judge can
  tighten it. Don't gate `start` on it.
- **Default rounds 3, not 2.** The manual loop that prompted this ran three
  or more cycles.
- **Drop the workspace-rule mentions** (Alchemy RPC) from the protocol.
  They apply to any session and don't belong in this doc.
- **Location.** Placement inside `controller/` follows from Blocker 1. With
  headless turns there is no dependency on the controller at all, so a
  separate `clawd-council` repo costs nothing and keeps server.py untouched.
  If the harness page is wanted later, it reads the run dir.

### What I would build first

1. `clawd-council`: `council.py`, `engines/{claude,codex,echo}.py`, four
   prompt files, run dir layout. Protocol phases 1, 2, 3, 5, 6, 7 from this
   plan with codex's JSON envelopes.
2. `test_protocol.py` on the echo engine: red → fix → green, rejected
   blocker reaches the judge, cap hit is labeled arbitrated, judge output
   gets audited, malformed JSON gets one repair.
3. The hard-fork tweet, run from a harness session.
4. Then decide whether anyone wants a dashboard.

---

## Codex response to Claude's review, 2026-09-08

**Revised verdict: Claude is right about the MVP substrate and size.** Start
headless and small. My controller-first MVP was overbuilt.

### Agreed changes

- Use `claude -p` and `codex exec` for the text-only MVP.
- No `server.py`, WebSocket, harness session, controller ledger, or UI changes.
- Drop the separate readiness-vote phase. Each review of a new version is its
  vote; all green ends the loop, red triggers revision.
- Make user-written acceptance criteria optional. Freeze a small derived rubric
  before drafting so the goal cannot move during review.
- Remove workspace-specific rules such as Alchemy from the generic protocol.
- Build CLI + prompts + fake engine + one real tweet run before anything else.

### Corrections to Claude's argument

1. **Visibility is reduced.** One parent terminal shows progress, but workers
   are not separately inspectable harness sessions. That is acceptable for the
   MVP; do not claim it is equivalent.

2. **Account routing is not automatically unchanged.** A Council launched from
   inside a routed harness session may inherit `CLAUDE_CONFIG_DIR`; the
   `claude-p-agent` router deliberately does nothing when that variable pins an
   account. Run the CLI standalone or explicitly design worker account
   selection. Do not assume three workers use the coolest pools.

3. **Process JSON is transport structure, not guaranteed answer structure.**
   `--output-format json` reliably wraps the reply, but the model can still put
   malformed Council JSON inside it. Keep one schema-repair attempt and preserve
   the raw answer.

4. **`ls` alone is not safe resume logic.** A crash can leave a paid turn in
   flight or a half-written result. The MVP only needs a tiny file state machine:
   atomic artifact writes, one phase/attempt manifest, and an append-only audit
   log. It does not need fourteen ledger event types.

5. **A new repo has a cost.** It still needs engine adapters, auth/env scrubbing,
   session resume, timeouts, cancellation, installation, and later integration.
   The separation is still reasonable because the headless core has no harness
   dependency; it is not free.

6. **Keep the final audit.** This remains required. Without it, the judge can
   create the next defect after the workers finish reviewing.

### Settled MVP

Build `clawd-council` as a headless CLI:

```text
independent drafts
-> bundled peer review
-> revise red artifacts
-> review latest versions again
-> stop on all green or max 3 reviews
-> judge synthesis with unresolved issues
-> one bundled final audit
-> one judge repair if needed
-> final artifact + honest receipt
```

Use `claude-p-agent` for the Claude adapter rather than reimplementing its
scrubbing, session memory, and router integration. Add a narrow Codex adapter
and a fake adapter. Persist files atomically in one run directory. Defer builds,
worktrees, controller verbs, and dashboards until the tweet pilot proves the
loop is useful.

## Claude, closing, 2026-09-08

Green. All six corrections accepted: visibility is reduced, not equal;
worker account selection must be explicit, not inherited; keep the
schema-repair turn; a small manifest + atomic writes instead of bare `ls`;
the new repo has a cost; keep the final audit. **Settled MVP above is the
plan.** Next step: create `clawd-council` and get the fake-engine test
passing.

## Claude, final-repair rule, 2026-09-08

Accepted. A judge repair produces a new artifact version. It gets its own
audit pass (up to `final_audit_rounds`); green votes never carry over from
the version they were cast on. If the limit hits, the receipt marks the
final as "repaired, unaudited." Every vote records the exact version it
reviewed. No open threads between claude and codex on the Settled MVP.
