"""The intent verbs — the controller's single tool surface.

Both consumers share this exact object: the MCP server (for an external agent)
and the in-process PM brain (for the chat UI). One surface, two front-ends — no
duplication. Read verbs are always allowed; write verbs (which touch the fleet)
pass through the autonomy gate + rate limiter + audit log.

The verbs are deliberately small and self-describing so a model can drive them
without ceremony: address sessions by (machine, cid), tasks by id, and every
write returns {ok, ...} or {ok:false, blocked|needs_confirm|error, ...}.
"""
import collections
import hashlib
import json
import re
import threading
import time
import urllib.parse

WRITE_VERBS = {"assign", "ask", "answer_prompt", "interrupt",
               "create_project", "clone_project", "spawn", "close",
               "start_pipeline", "pin", "add_local_project", "remove_project"}

# Engines a session can run under (server.py ENGINES). A missing/unknown value
# means claude everywhere — old registry rows and pre-engine harnesses.
ENGINES = ("claude", "codex")

# Belt-and-braces ceiling for read-verb replies: the brain's tool-output budget
# is ~25k tokens; anything we hand it must stay comfortably under that even if
# the world grows 10x. get_world degrades (drop digests → counts-only) rather
# than ever exceeding it.
WORLD_CHAR_BUDGET = 20_000
FIND_LIMIT_MAX = 40
FIND_FANOUT_BUDGET_S = 12.0

# Pipeline bounds. Steps are cheap to add and expensive to run (one session
# each), and every prior step's output is folded into the next step's kickoff —
# so both the fan-out and the prompt size need a ceiling.
PIPE_MAX_STEPS = 6
PIPE_CTX_CHARS = 2_500          # per prior step, folded into a kickoff
PIPE_CTX_TOTAL = 9_000          # all prior steps together
STEP_OUTPUT_CHARS = 4_000       # what we record as a step's result


class Guard:
    """Autonomy + rate limiting for write verbs."""

    def __init__(self, autonomy="confirm", rate_per_min=8):
        self.autonomy = autonomy            # readonly | confirm | auto
        self.rate_per_min = rate_per_min
        self._hits = collections.defaultdict(collections.deque)

    def rate_ok(self, key):
        now = time.time()
        dq = self._hits[key]
        while dq and now - dq[0] > 60:
            dq.popleft()
        if len(dq) >= self.rate_per_min:
            return False
        dq.append(now)
        return True


class Verbs:
    def __init__(self, world, ledger, clients, guard, notes=None):
        self.world = world
        self.ledger = ledger
        self.clients = clients
        self.guard = guard
        self.notes = notes                # NotesStore (PM durable memory), optional
        self.escalate_sink = None         # set by serve: Autopilot.escalate

    # ── read ──────────────────────────────────────────────────────────────
    def get_world(self, machine=None, pid=None, verbose=False):
        snap = self.world.snapshot(machine=machine, pid=pid, verbose=verbose)
        # Nothing this verb returns may blow the tool-output budget — the raw
        # fleet snapshot once hit 66KB and the PM's primary sense organ died.
        if len(json.dumps(snap)) > WORLD_CHAR_BUDGET:
            for m in snap["machines"]:
                for p in m.get("projects", []):
                    for s in p.get("sessions", []):
                        s.pop("digest", None)
            snap["truncated"] = True
        if len(json.dumps(snap)) > WORLD_CHAR_BUDGET:
            snap = {"machines": [
                        {k: m.get(k) for k in ("id", "connected", "sessions",
                                               "blocked", "working", "idle")}
                        for m in snap["machines"]],
                    "truncated": True,
                    "hint": "too big even compacted — drill down with "
                            "get_world(machine=…) / get_world(machine=…, pid=…)"}
        return snap

    def get_attention(self):
        return {"items": self.world.attention()}

    def session_digest(self, machine, cid):
        return self.world.session_detail(machine, cid)

    def find(self, query, machine=None, scope="all", limit=20):
        """One call answers "which session/task/project has to do with X":
        the task ledger + cached session/project meta locally (works even for
        offline machines), plus a server-side transcript search fanned out to
        every connected machine. Deterministic, bounded, deep links attached."""
        q = (query or "").strip()
        if not q:
            return {"ok": False, "error": "empty query"}
        ql = q.lower()
        limit = max(1, min(int(limit or 20), FIND_LIMIT_MAX))
        matches, seen = [], set()

        def add(m):
            key = (m.get("machine"), m.get("cid") or m.get("task_id"), m.get("where"))
            if key not in seen:
                seen.add(key)
                matches.append(m)

        # 1) task ledger — local, no I/O
        for t in self.ledger.list_tasks():
            hay = " ".join(filter(None, [
                t.get("goal"), t.get("project"), t.get("acceptance"),
                " ".join(h.get("event", "") for h in t.get("history", []))]))
            if ql in hay.lower():
                add({"where": "task", "task_id": t["id"],
                     "status": t.get("status"), "machine": t.get("machine"),
                     "snippet": (t.get("goal") or "")[:160],
                     "sessions": t.get("sessions", [])[-3:]})
        # 2) cached session/project meta — includes offline machines
        clients = {mid: c for mid, c in self.clients.items()
                   if not machine or mid == machine}
        for mid, c in clients.items():
            st = c.state()
            for p in st["projects"]:
                name = p.get("name") or ""
                if ql in name.lower():
                    add({"where": "project", "machine": mid, "pid": p.get("pid"),
                         "snippet": name[:160],
                         "url": self._harness_link(p.get("pid"), machine=mid)["url"]})
            for s in st["sessions"]:
                for where, val in (("title", s.get("title")),
                                   ("desc", s.get("desc")),
                                   ("digest", s.get("digest")),
                                   ("blocked_on", s.get("blocked_on")),
                                   ("test_hint", s.get("testHint")),
                                   ("lastAnswer", s.get("lastAnswer"))):
                    if val and ql in val.lower():
                        add({"where": where, "machine": mid, "cid": s["cid"],
                             "pid": s.get("pid"),
                             "title": (s.get("title") or s["cid"])[:60],
                             "snippet": val[:160],
                             **engine_tag(s),
                             "url": self._harness_link(s.get("pid"), s["cid"],
                                                       machine=mid)["url"]})
                        break               # one meta hit per session is plenty
        # 3) transcript fan-out — server-side search on each connected machine
        unreachable = []
        if scope in ("transcript", "all"):
            live = [(mid, c) for mid, c in clients.items()
                    if getattr(c, "connected", False)]
            per = max(3, limit // len(live)) if live else 0
            results = {}

            def _one(mid, c):
                results[mid] = c.search(q, scope="transcript", limit=per)

            threads = [threading.Thread(target=_one, args=(mid, c), daemon=True)
                       for mid, c in live]
            for th in threads:
                th.start()
            deadline = time.time() + FIND_FANOUT_BUDGET_S
            for th in threads:
                th.join(timeout=max(0.1, deadline - time.time()))
            for mid, _c in live:
                r = results.get(mid)
                if not isinstance(r, dict) or r.get("error"):
                    unreachable.append(mid)
                    continue
                # the harness's search reply has no engine field — resolve it
                # from our own session cache so a codex hit still says so
                cached = {s["cid"]: s for s in _c.state()["sessions"]}
                for hit in r.get("matches", []):
                    add({"where": "transcript", "machine": mid,
                         "cid": hit.get("cid"), "pid": hit.get("pid"),
                         "title": (hit.get("title") or "")[:60],
                         "snippet": (hit.get("snippet") or "")[:160],
                         **engine_tag(cached.get(hit.get("cid")) or {}),
                         "url": self._harness_link(hit.get("pid"), hit.get("cid"),
                                                   machine=mid)["url"]})
        # precise sources first, transcript hits last
        order = {"title": 0, "task": 1, "project": 2, "desc": 3, "digest": 4,
                 "blocked_on": 5, "lastAnswer": 6, "transcript": 7}
        matches.sort(key=lambda m: order.get(m.get("where"), 9))
        out = {"ok": True, "query": q, "matches": matches[:limit],
               "truncated": len(matches) > limit}
        if unreachable:
            out["unreachable"] = unreachable
        return out

    def transcript_tail(self, machine, cid, n=30):
        """Last n structured transcript events for one session — what it
        actually said/did. Read this before answer_prompt: a pending
        AskUserQuestion's options ride in its tool_use event."""
        c = self.clients.get(machine)
        if not c:
            return {"ok": False, "error": f"no such machine: {machine}"}
        n = max(1, min(int(n or 30), 50))
        r = c.transcript_tail(cid, n=n)
        if not isinstance(r, dict) or r.get("error"):
            return {"ok": False,
                    "error": (r or {}).get("error") or "no reply from machine"}
        return {"ok": True, "machine": machine, "cid": cid,
                "events": r.get("events", [])}

    def peek_screen(self, machine, cid):
        """De-ANSI'd render of a session's current terminal screen — for TUI
        dialogs that never reach the transcript (trust prompts, menus)."""
        c = self.clients.get(machine)
        if not c:
            return {"ok": False, "error": f"no such machine: {machine}"}
        r = c.screen(cid)
        if not isinstance(r, dict) or r.get("error"):
            return {"ok": False,
                    "error": (r or {}).get("error") or "no reply from machine"}
        return {"ok": True, "machine": machine, "cid": cid,
                "text": r.get("text", ""), "cols": r.get("cols"),
                "rows": r.get("rows")}

    def sweep(self, max_items=20):
        """The one-call check-in bundle: the attention queue enriched with
        evidence (a short transcript tail for high items), deep links, and a
        suggested clearing verb — plus rollups (sessions actively working
        right now, idle sessions with no task, in-progress tasks whose
        sessions are gone). Read-only: acting on it stays with the persona +
        the autonomy gate."""
        max_items = max(1, min(int(max_items or 20), 30))
        items = self.world.attention(limit=max_items)
        enriched = 0
        for it in items:
            it["url"] = self._harness_link(it.get("pid"), it["cid"],
                                           machine=it["machine"])["url"]
            it["clear_with"] = {"verb": it.get("suggested_action"),
                                "args": {"machine": it["machine"], "cid": it["cid"]}}
            if it["sev"] == "high" and enriched < 10:
                enriched += 1
                c = self.clients.get(it["machine"])
                r = c.transcript_tail(it["cid"], n=3, chars=200) if c else None
                if isinstance(r, dict) and not r.get("error"):
                    it["tail"] = r.get("events", [])
        idle_no_task, stale_tasks, working = [], [], []
        live_cids = set()
        for mid, c in self.clients.items():
            for s in c.state()["sessions"]:
                live_cids.add(s["cid"])
                st = self.world._status(s)
                if st in ("working", "background"):
                    # actively-running work: without this rollup a sweep-driven
                    # "how's everything?" answer literally cannot see sessions
                    # that are mid-turn (they're not attention, not idle) and
                    # reads a busy fleet as a quiet one
                    row = {"machine": mid, "cid": s["cid"], "status": st,
                           "title": (s.get("title") or s["cid"])[:60]}
                    if s.get("digest"):
                        row["digest"] = s["digest"][:80]
                    working.append(row)
                elif (st == "idle" and not s.get("pinned")
                        and not self.ledger.task_for_cid(s["cid"])):
                    idle_no_task.append({"machine": mid, "cid": s["cid"],
                                         "title": (s.get("title") or s["cid"])[:60]})
        for t in self.ledger.list_tasks("in_progress"):
            if t.get("sessions") and not any(x in live_cids for x in t["sessions"]):
                stale_tasks.append({"task_id": t["id"],
                                    "goal": (t.get("goal") or "")[:100]})
        counts = {}
        for it in items:
            counts[it["sev"]] = counts.get(it["sev"], 0) + 1
        return {"ok": True, "counts": counts, "items": items,
                "working": working[:15],
                "idle_no_task": idle_no_task[:15],
                "stale_tasks": stale_tasks[:10]}

    def get_accounts(self, machine=None):
        """Per-machine subscription health: which Claude login new sessions
        spawn under, how much of each plan window is spent, when it resets —
        plus codex's plan (read-only; codex is single-login and sits outside the
        router). This is how "is that machine about to run out of plan?" gets
        answered without asking the operator to open the 🧠 page.

        Read-only by design and there is deliberately NO verb to switch
        accounts: the harness routes itself (docs/fleet/SUB-ROUTING.md) and is
        better at it than a turn-based PM — it watches usage continuously and
        hands sessions off mid-flight. Your job is to know, and to say so when a
        machine has no headroom left.

        A pool carrying `routable: false` is one the router SKIPS regardless of
        headroom (its plan can't do fable). Don't count it as spare capacity —
        a machine whose only free pool is unroutable is a machine in trouble.

        **Read `pools`, not `accounts`, when you report health.** An `accounts`
        row is a config-dir LABEL, and one subscription routinely wears several
        of them on one machine — so counting rows double-counts capacity, and
        counting `needs-login` rows invents outages that don't exist (a dead
        label whose org is signed in under another label costs nothing). The
        `pools` list collapses labels by org uuid: `plans_total` is how many
        subscriptions the machine actually holds and `plans_usable` how many the
        router can spend right now. Only say a plan needs a re-sign-in when its
        pool has `live: false` — every label into it is dead."""
        out = []
        for mid, c in self.clients.items():
            if machine and mid != machine:
                continue
            a = c.state().get("accounts") or {}
            if not a:
                out.append({"machine": mid, "known": False,
                            "hint": "no accounts frame seen yet — link just came "
                                    "up, or that harness predates accounts"})
                continue
            rows = []
            for acc in a.get("accounts") or []:
                row = {"name": acc.get("name"), "status": acc.get("status"),
                       "active": bool(acc.get("active")),
                       # The label is a FOLDER, not a subscription. One plan
                       # commonly wears several labels on one machine, so the
                       # org uuid rides along on every row — see `pools`.
                       "org": acc.get("orgUuid") or "",
                       "plan": acc.get("orgName") or "",
                       "usage_pct": acc.get("usagePct"),
                       "headroom": acc.get("headroom")}
                # Capacity is not the only way a pool goes unusable: a plan
                # that stopped carrying fable is skipped by the router at any
                # headroom (SUB_REQUIRE_FABLE). Without this the PM reads a
                # greyed-out 97%-free pool as spare capacity and reports the
                # machine as healthy while the router refuses to spend it.
                if acc.get("routable") is False:
                    row["routable"] = False
                    row["skipped_because"] = ("plan doesn't carry fable — the "
                                              "router won't spawn or hand off here")
                wins = [w for w in (acc.get("windows") or []) if w.get("resets")]
                if wins:
                    row["windows"] = [{"label": w.get("label"), "used": w.get("used"),
                                       "resets": w.get("resets")} for w in wins[:3]]
                if acc.get("error"):
                    row["error"] = str(acc["error"])[:120]
                rows.append(row)
            # Collapse labels to actual SUBSCRIPTIONS. Counting `accounts`
            # rows overstates both capacity and breakage: on clawd-heart seven
            # labels are four plans, and two of the three "needs-login" rows
            # are duplicate logins into orgs that are signed in under another
            # label — i.e. nothing is actually offline. Report pools, and only
            # call a plan dead when EVERY label into it is dead.
            pools = {}
            for r in rows:
                key = r.get("org") or f"?{r['name']}"
                pl = pools.setdefault(key, {
                    "plan": r.get("plan") or r["name"], "org": r.get("org") or "",
                    "labels": [], "live": False, "routable": True,
                    "usage_pct": None})
                pl["labels"].append(r["name"])
                if r["status"] == "ready":
                    pl["live"] = True
                    if r.get("routable") is False:
                        pl["routable"] = False
                    if r.get("usage_pct") is not None and (
                            pl["usage_pct"] is None
                            or r["usage_pct"] < pl["usage_pct"]):
                        pl["usage_pct"] = r["usage_pct"]
            pool_rows = list(pools.values())
            m = {"machine": mid, "known": True, "active": a.get("active"),
                 "auto_switch": bool(a.get("auto")),
                 "would_spawn_on": a.get("best"),
                 "pools": pool_rows,
                 "plans_total": len(pool_rows),
                 "plans_usable": sum(1 for p in pool_rows
                                     if p["live"] and p["routable"]),
                 "accounts": rows}
            cx = a.get("codex") or {}
            if cx:
                m["codex"] = {"status": cx.get("status"), "plan": cx.get("plan"),
                              "usage_pct": cx.get("pct"),
                              "limit_reached": cx.get("limitReached") or "",
                              "routed": False}
            out.append(m)
        return {"ok": True, "machines": out}

    def get_pins(self, machine=None):
        """The 📌 pin board: sessions parked as "coded, not yet verified". Each
        carries `test_hint` — one imperative instruction for a HUMAN to go and
        check ("open /eq during the next show and verify 30fps"). A pinned
        session is fully alive and still promptable; it just left the tab strip.

        Pins are the fleet's done-but-unverified queue, so this is the honest
        answer to "what's outstanding?" that neither the attention queue (they
        aren't blocked) nor idle_no_task (they're deliberately parked) gives."""
        pins = []
        for mid, c in self.clients.items():
            if machine and mid != machine:
                continue
            for s in c.state()["sessions"]:
                if not s.get("pinned"):
                    continue
                pins.append({"machine": mid, "cid": s["cid"], "pid": s.get("pid"),
                             "title": (s.get("title") or s["cid"])[:60],
                             "pinned_at": s.get("pinned"),
                             "test_hint": (s.get("testHint") or "")[:300],
                             "digest": (s.get("digest") or "")[:120],
                             "status": self.world._status(s),
                             **engine_tag(s),
                             "url": self._harness_link(s.get("pid"), s["cid"],
                                                       machine=mid)["url"]})
        pins.sort(key=lambda p: p.get("pinned_at") or 0, reverse=True)
        return {"ok": True, "count": len(pins), "pins": pins[:30]}

    def list_tasks(self, status=None):
        tasks = self.ledger.list_tasks(status)[:50]
        for t in tasks:                       # full history stays in get_task
            if len(t.get("history", [])) > 3:
                t["history"] = t["history"][-3:]
            if t.get("steps"):
                # a step's output runs to pages — a listing is a menu, not a read
                t["steps"] = [f"{s['n']} {s.get('role') or 'step'}/"
                              f"{_norm_engine(s.get('engine'))} "
                              f"{s.get('status') or 'pending'}"
                              for s in t["steps"]]
        return {"tasks": tasks}

    # ── operator channel + durable memory (always allowed — no fleet writes) ──
    def escalate(self, question, machine=None, cid=None, urgency="digest"):
        """Queue something for the operator. 'digest' rides the next batched
        push (one message per window, not a ping per item); 'now' pushes
        immediately — reserve it for actively-breaking things."""
        if not (question or "").strip():
            return {"ok": False, "error": "empty question"}
        if self.escalate_sink is None:
            return {"ok": False, "error": "no escalation channel here — put the "
                    "question in your reply instead (the operator is reading it)"}
        item = {"question": question.strip()[:500], "machine": machine, "cid": cid,
                "urgency": "now" if urgency == "now" else "digest"}
        if machine and cid:
            s = self.world.session_detail(machine, cid)
            if not s.get("error"):
                item["url"] = self._harness_link(s.get("pid"), cid,
                                                 machine=machine)["url"]
        return self.escalate_sink(item)

    def remember_note(self, text, scope="general"):
        """Write to your own durable memory (rendered into every future turn).
        Scopes: machine:<id>, project:<name>, task:<id>, general."""
        if not self.notes:
            return {"ok": False, "error": "notes store not configured"}
        return self.notes.remember(scope, text)

    def forget_note(self, scope, index):
        if not self.notes:
            return {"ok": False, "error": "notes store not configured"}
        return self.notes.forget(scope, index)

    def get_notes(self):
        if not self.notes:
            return {"ok": False, "error": "notes store not configured"}
        return {"ok": True, **self.notes.dump()}

    def set_priorities(self, priorities):
        """Operator-stated standing priorities (ordered). Set ONLY when the
        operator states/changes them — they steer every future turn."""
        if not self.notes:
            return {"ok": False, "error": "notes store not configured"}
        return self.notes.set_priorities(priorities)

    GET_TASK_STEP_CHARS = 1_200

    def get_task(self, task_id):
        t = self.ledger.get(task_id)
        if not t:
            return {"error": f"no such task: {task_id}"}
        for s in t.get("steps") or []:
            s.pop("sent", None)               # the kickoff we sent; noise on read
            out = s.get("output") or ""
            if len(out) > self.GET_TASK_STEP_CHARS:
                s["output"] = out[:self.GET_TASK_STEP_CHARS]
                s["output_chars"] = len(out)
                s["more"] = f"get_step_output('{task_id}', {s['n']}) for all of it"
        return t

    def get_step_output(self, task_id, n):
        """One pipeline step's full recorded answer — get_task truncates them so
        a 6-step read can't blow the budget. This is where the final report is."""
        t = self.ledger.get(task_id)
        if not t:
            return {"ok": False, "error": f"no such task: {task_id}"}
        s = _find_step(t, int(n))
        if not s:
            return {"ok": False, "error": f"{task_id} has no step {n}"}
        return {"ok": True, "task": task_id, "step": s["n"],
                "role": s.get("role"), "engine": _norm_engine(s.get("engine")),
                "status": s.get("status"), "cid": s.get("cid"),
                "output": s.get("output") or ""}

    def create_task(self, goal, project=None, acceptance="", machine=None):
        # Pure bookkeeping (no fleet action) → always allowed, even read-only.
        t = self.ledger.create_task(goal, project, acceptance, machine)
        return {"ok": True, "task": t}

    def set_task_status(self, task_id, status):
        t = self.ledger.set_status(task_id, status)
        return {"ok": bool(t), "task": t} if t else {"ok": False, "error": "no such task"}

    def note_task(self, task_id, text):
        t = self.ledger.note(task_id, text)
        return {"ok": bool(t), "task": t} if t else {"ok": False, "error": "no such task"}

    # ── pipelines: one task, N ordered steps, a session each ──────────────────
    # A PM turn ends when it replies, so "research it on claude, have codex
    # double-check that, then let claude write the final report" had nowhere to
    # live: the only unattended follow-up was the autopilot's verify turn, which
    # is single-shot and forbidden from spawning. A pipeline records the whole
    # plan up front; `advance_pipeline` runs the next step with the previous
    # steps' answers folded into its prompt, and the autopilot calls it the
    # moment a step's session finishes a turn. That call is deterministic (no
    # LLM turn), so a long chain costs nothing but the sessions themselves.

    def create_pipeline(self, goal, steps, project=None, acceptance="", machine=None):
        """Record a multi-step plan: `steps` is an ordered list of
        `{role, engine, prompt, pid?, reuse?}` dicts (≤6).

        - `role`   — a short label ("research", "review", "report").
        - `engine` — "claude" (default) or "codex" for this step's session.
        - `prompt` — what to ask. Every prior step's final answer is folded in
          automatically, so write it as "double-check the research above", not
          "double-check {}".
        - `pid`    — the project to spawn in (falls back to start_pipeline's).
        - `reuse`  — an earlier step number: send this step's prompt to THAT
          step's session instead of spawning a fresh one. This is how "claude
          takes codex's feedback and rewrites" keeps its own research in
          context. The only sanctioned session reuse — it's the same task's own
          session, not someone else's work.

        Bookkeeping only: nothing touches the fleet until `start_pipeline`."""
        norm, err = _norm_steps(steps)
        if err:
            return {"ok": False, "error": err}
        t = self.ledger.create_pipeline(goal, norm, project, acceptance, machine)
        return {"ok": True, "task": t,
                "hint": f"start_pipeline('{t['id']}') runs step 1; every later "
                        "step fires by itself as the one before it finishes"}

    def start_pipeline(self, task_id, machine=None, pid=None, confirm=False):
        """Run step 1 of a pipeline. WRITE — this is the ONE approval point for
        the whole chain: confirming it approves every step in the plan, which is
        why the later steps advance without asking again. `machine`/`pid` supply
        defaults for steps that don't name their own."""
        def do():
            t = self.ledger.get(task_id)
            if not t:
                return {"ok": False, "error": f"no such task: {task_id}"}
            if not t.get("pipeline"):
                return {"ok": False, "error": f"{task_id} is a plain task — use "
                        "assign, or create_pipeline for a multi-step plan"}
            if t.get("step"):
                return {"ok": False, "error": f"{task_id} already started "
                        f"(at step {t['step']}) — advance_pipeline continues it"}
            if machine:
                t["machine"] = machine
            return self._run_step(t, 1, default_pid=pid)
        return self._gate("start_pipeline", {"task_id": task_id, "machine": machine,
                                             "pid": pid, "confirm": confirm}, do)

    def advance_pipeline(self, task_id, force=False):
        """Close out the pipeline's running step (recording its session's final
        answer as that step's output) and run the next one. On the last step:
        mark the task `review` and return the final report.

        Deliberately NOT behind the confirm gate — `start_pipeline` was the
        approval, and these are the steps it approved. It still refuses under
        autonomy=readonly, is rate limited, and is audited like any write. It
        also refuses to run step 1 (an unstarted pipeline), so the gated
        approval can't be skipped by calling this first.

        `force` closes a step whose session produced nothing readable — the
        escape hatch for an engine whose turn-end hook never fired."""
        if self.guard.autonomy == "readonly":
            return {"ok": False, "blocked": "controller is read-only"}
        if not self.guard.rate_ok(f"pipeline:{task_id}"):
            return {"ok": False,
                    "blocked": f"rate limit ({self.guard.rate_per_min}/min) for {task_id}"}
        result = self._advance(task_id, force=force)
        self.ledger.audit("advance_pipeline", {"task_id": task_id, "force": force},
                          result)
        return result

    def _advance(self, task_id, force=False):
        t = self.ledger.get(task_id)
        if not t:
            return {"ok": False, "error": f"no such task: {task_id}"}
        if not t.get("pipeline"):
            return {"ok": False, "error": f"{task_id} is not a pipeline"}
        steps = t.get("steps") or []
        running = next((s for s in steps if s.get("status") == "running"), None)
        if not running:
            if not t.get("step"):
                return {"ok": False, "error": f"{task_id} hasn't started — "
                        "start_pipeline runs step 1 (and is the approval gate)"}
            return {"ok": False, "error": f"{task_id} has no running step "
                    f"(status {t.get('status')})"}
        machine = running.get("machine") or t.get("machine")
        out, src = self._step_output(machine, running.get("cid"))
        if out and running.get("base") and out_hash(out) == running["base"]:
            out, src = "", "unchanged"      # still the answer that was already there
        if not out and not force:
            return {"ok": False, "error": f"step {running['n']}'s session "
                    f"({machine}/{running.get('cid')}) has produced no readable "
                    "answer yet — read it with transcript_tail, or pass "
                    "force=true to close the step empty and move on",
                    "step": running["n"], "cid": running.get("cid"),
                    "output_state": src}
        self.ledger.finish_step(task_id, running["n"], out,
                               status="done" if out else "failed")
        t = self.ledger.get(task_id)
        nxt = next((s for s in t.get("steps") or []
                    if s.get("status") == "pending"), None)
        if not nxt:
            self.ledger.set_status(task_id, "review")
            return {"ok": True, "task": task_id, "complete": True,
                    "steps_run": len(t.get("steps") or []),
                    "output_from": src,
                    "report": out[:PIPE_CTX_CHARS],
                    "hint": "pipeline finished — task is in `review`. Hand the "
                            "report to the operator (escalate, or say it in chat)."}
        run = self._run_step(t, nxt["n"])
        return {"ok": run.get("ok", False), "task": task_id,
                "finished_step": running["n"], "output_from": src,
                "next": run}

    def _run_step(self, task, n, default_pid=None):
        """Spawn (or reuse) step n's session and send it its prompt. The caller
        owns the autonomy decision; this is the mechanics."""
        step = _find_step(task, n)
        if not step:
            return {"ok": False, "error": f"{task['id']} has no step {n}"}
        machine = step.get("machine") or task.get("machine")
        c = self.clients.get(machine)
        if not c:
            return {"ok": False, "error": f"no machine {machine!r} for step {n} — "
                    "set `machine` on the pipeline or pass it to start_pipeline"}
        engine = _norm_engine(step.get("engine"))
        live = {s["cid"]: s for s in c.state()["sessions"]}
        reuse, spawned = step.get("reuse"), False
        if reuse:
            prev = _find_step(task, reuse)
            cid = (prev or {}).get("cid")
            if not cid:
                return {"ok": False, "error": f"step {n} reuses step {reuse}, "
                        "which never ran"}
            if cid not in live:
                return {"ok": False, "error": f"step {n} reuses step {reuse}'s "
                        f"session ({cid}), which is gone — drop the `reuse` and "
                        "let it spawn fresh"}
            engine = _norm_engine(live[cid].get("engine"))
        else:
            pid = step.get("pid") or default_pid
            if not pid:
                return {"ok": False, "error": f"step {n} has no project — set "
                        "`pid` on the step or pass pid= to start_pipeline"}
            cid = c.new_session(pid, engine=engine)
            if not cid:
                return {"ok": False, "error": f"step {n}: no {engine} session "
                        "came back (spawn timed out)"}
            spawned = True
            live = {s["cid"]: s for s in c.state()["sessions"]}
        pid_used = (live.get(cid) or {}).get("pid") or step.get("pid") or default_pid
        # Baseline: what this session's "final answer" reads as BEFORE we prompt
        # it. A step that reuses an earlier step's session already has an answer
        # sitting there, and without this the advance logic would read that stale
        # text as this step's output and skip straight past the work.
        pre = "" if spawned else self._step_output(machine, cid)[0]
        kickoff = self._step_kickoff(task, step)
        self.ledger.start_step(task["id"], n, cid, machine=machine, engine=engine,
                               prompt=kickoff, base=out_hash(pre))
        if not c.send_message(cid, kickoff):
            self.ledger.note(task["id"], f"step {n}: session {cid} would not take "
                             "the prompt (link down?)")
            return {"ok": False, "error": f"step {n}: session {cid} would not take "
                    "the prompt", "cid": cid}
        return {"ok": True, "task": task["id"], "step": n,
                "role": step.get("role"), "engine": engine, "machine": machine,
                "cid": cid, "spawned": spawned, "reused_step": reuse,
                "url": self._harness_link(pid_used, cid, machine=machine)["url"]}

    def _step_kickoff(self, task, step):
        """The prompt a step's session actually receives: what it is, the
        overall goal, its own instruction, and every earlier step's answer."""
        n, total = step["n"], len(task.get("steps") or [])
        eng = _norm_engine(step.get("engine"))
        lines = [f"[pipeline {task['id']} · step {n}/{total} · "
                 f"role: {step.get('role') or 'step'} · engine: {eng}]"]
        if task.get("goal"):
            lines.append(f"Overall goal: {task['goal']}")
        lines += ["", step.get("prompt") or task.get("goal") or ""]
        prior = self._prior_context(task, n)
        if prior:
            lines += ["", prior]
        if n == total and task.get("acceptance"):
            lines += ["", f"Done when: {task['acceptance']}"]
        lines += ["", "(You are one step of a pipeline the fleet PM is running. "
                  "Your FINAL message in this session is what gets handed to the "
                  "next step — put the deliverable in it, not only in a file.)"]
        return "\n".join(lines)

    @staticmethod
    def _prior_context(task, n):
        """Earlier steps' outputs, newest-first until the budget runs out, then
        presented in order. Newest-first because when a long chain overflows,
        the step just before you matters more than the one four back."""
        picked, total = [], 0
        for s in sorted((task.get("steps") or []), key=lambda x: -x.get("n", 0)):
            if s.get("n") >= n or s.get("status") != "done":
                continue
            out = (s.get("output") or "").strip()
            if not out:
                continue
            block = (f"── step {s['n']} ({s.get('role') or 'step'}, "
                     f"{_norm_engine(s.get('engine'))}) answered ──\n"
                     f"{out[:PIPE_CTX_CHARS]}")
            if total + len(block) > PIPE_CTX_TOTAL:
                picked.append(f"(earlier steps trimmed — full text in "
                              f"get_task('{task['id']}'))")
                break
            picked.append(block)
            total += len(block)
        return "\n\n".join(reversed(picked))

    def _step_output(self, machine, cid):
        """A finished step's deliverable: its session's final assistant message.
        Prefers the durable `last_answer` (Stop-hook capture), and falls back to
        the newest assistant text in the transcript — which is what carries a
        step whose turn-end hook went missing (on a box running two harnesses,
        codex sessions share one hooks file and the loser's signal is silently
        lost — docs/CODEX-ENGINE.md). n=20 rather than a small tail, because a
        codex transcript ends in token-count bookkeeping the parser drops.
        Returns (text, source)."""
        det = self.world.session_detail(machine, cid)
        if isinstance(det, dict):
            out = (det.get("last_answer") or "").strip()
            if out:
                return out[:STEP_OUTPUT_CHARS], "last_answer"
        r = self.transcript_tail(machine, cid, n=20)
        if r.get("ok"):
            for ev in reversed(r.get("events") or []):
                if ev.get("role") == "assistant":
                    txt = (ev.get("text") or "").strip()
                    if txt:
                        return txt[:STEP_OUTPUT_CHARS], "transcript"
        return "", "none"

    # ── navigate: "send me to" a session/project in the harness UI ────────────
    # Read-only — these build a deep link, they don't touch the fleet. The chat
    # UI renders any result carrying `nav:true` as an "Open ↗" button (and the
    # url is in the reply text too, so Telegram/non-browser clients still get it).
    @staticmethod
    def _norm_repo(url):
        """Mirror of index.html normRepo() (and fleet/worker.py _norm_repo):
        canonicalize a git remote so the same repo unifies across machines.
        MUST stay byte-identical to the JS or the deep-link projectKey won't
        match the one the fleet UI routes on."""
        s = (url or "").strip()
        if not s:
            return ""
        s = re.sub(r"^git@([^:]+):", r"\1/", s)            # git@host:owner/repo → host/owner/repo
        s = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", s)  # strip scheme
        s = re.sub(r"\.git$", "", s, flags=re.I)            # drop trailing .git
        s = re.sub(r"/+$", "", s)                           # drop trailing slash
        return s.lower()

    def _project_key(self, machine, pid):
        """The unified projectKey the FLEET UI routes on — NOT the machine-local
        pid. `unifiedProjects` in index.html is keyed by repo (so one project
        that lives on N machines is one card), so a hash carrying a raw pid
        finds no group and bounces you to the projects list. Local (private
        folder) projects are machine-qualified. Mirror of index.html
        projectKey() / fleet/worker.py _project_key()."""
        c = self.clients.get(machine)
        p = None
        if c:
            p = next((x for x in c.state()["projects"] if x.get("pid") == pid), None)
        if not p:
            return None
        if p.get("kind") == "local":
            return f"local:{machine}:{p.get('path') or p.get('name') or ''}"
        return self._norm_repo(p.get("repoUrl")) or ("name:" + (p.get("name") or ""))

    def _harness_link(self, pid=None, cid=None, view="transcript", machine=None):
        """Build the deep link into the harness UI. Hash route mirrors index.html:
        direct mode  `#/p/<pid>/s/<cid>` ; fleet/box mode is machine-prefixed AND
        keyed by projectKey, not pid — `#/m/<machine>/p/<projectKey>/s/<cid>`
        (`…/tty` for the terminal). Returns an absolute `url` plus a host-relative
        `path` (+ `port`) so the browser can rebuild it against its own origin —
        see pmNavHref()/navHref() in the UI. Full grammar: docs/DEEPLINKS.md.

        Fleet mode (CONTROLLER_RELAY set): the UI is served by the public relay at
        its own origin under a passkey, so the link drops the harness `?t=` token
        and the box-internal :8788 port (`port=None` → the browser rebuilds against
        the public origin it's already viewing). Direct mode keeps the token+port."""
        from . import config
        seg = ""
        if config.fleet_mode():
            if machine:
                seg = "m/" + urllib.parse.quote(machine, safe="") + "/"
            # Fleet routes on the unified projectKey; a pid here silently lands
            # the user on the projects list. If we can't resolve one (machine
            # gone / project not in the cache yet), drop the project segment
            # rather than emit a hash that can't resolve — the machine prefix
            # alone still gets them to the right box.
            pid = self._project_key(machine, pid) if pid else None
        if cid and pid:
            frag = f"#/{seg}p/{urllib.parse.quote(pid, safe='')}/s/{cid}" + ("/tty" if view == "tty" else "")
        elif pid:
            frag = f"#/{seg}p/{urllib.parse.quote(pid, safe='')}"
        else:
            frag = f"#/{seg}"
        if config.fleet_mode():
            path = "/" + frag                       # → /#/m/<machine>/…  (host-relative)
            return {"url": config.public_ui_base() + path, "path": path, "port": None}
        base = config.harness_http_base()
        token = config.harness_token()
        path = (f"/?t={urllib.parse.quote(token)}" if token else "/") + frag
        parsed = urllib.parse.urlparse(base)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return {"url": base + path, "path": path, "port": port}

    def open_session(self, machine, cid, view="transcript"):
        """Deep link that jumps the user's browser straight to one session
        (transcript, or its terminal with view='tty'). Use for 'take me to' /
        'open' / 'send me to' a session."""
        s = self.world.session_detail(machine, cid)
        if s.get("error"):
            return {"ok": False, "error": s["error"]}
        pid = s.get("pid")
        link = self._harness_link(pid, cid, view, machine=machine)
        return {"ok": True, "nav": True, "machine": machine, "pid": pid,
                "cid": cid, "view": view, "title": s.get("title") or cid, **link}

    def open_project(self, machine, pid):
        """Deep link to a project's session list in the harness UI."""
        c = self.clients.get(machine)
        if not c:
            return {"ok": False, "error": f"no such machine: {machine}"}
        proj = next((p for p in c.state()["projects"] if p.get("pid") == pid), None)
        if not proj:
            return {"ok": False, "error": f"no such project: {pid}"}
        link = self._harness_link(pid, machine=machine)
        return {"ok": True, "nav": True, "machine": machine, "pid": pid,
                "name": proj.get("name") or pid, **link}

    # ── write (gated) ───────────────────────────────────────────────────────
    def _gate(self, verb, args, do):
        if verb in WRITE_VERBS:
            if self.guard.autonomy == "readonly":
                return {"ok": False, "blocked": "controller is read-only",
                        "hint": "set CONTROLLER_AUTONOMY=confirm or auto to enable writes",
                        "proposed": {"verb": verb, "args": _clean(args)}}
            if self.guard.autonomy == "confirm" and not args.get("confirm"):
                return {"ok": False, "needs_confirm": True,
                        "proposed": {"verb": verb, "args": _clean(args)},
                        "hint": "re-call with confirm=true to execute"}
            key = args.get("cid") or args.get("machine") or verb
            if not self.guard.rate_ok(key):
                return {"ok": False, "blocked": f"rate limit ({self.guard.rate_per_min}/min) for {key}"}
        result = do()
        self.ledger.audit(verb, _clean(args), result)
        return result

    def ask(self, machine, cid, text, confirm=False):
        def do():
            c = self.clients.get(machine)
            if not c:
                return {"ok": False, "error": f"no such machine: {machine}"}
            return {"ok": c.send_message(cid, text), "machine": machine,
                    "cid": cid, "sent": text}
        return self._gate("ask", {"machine": machine, "cid": cid, "text": text,
                                  "confirm": confirm}, do)

    def assign(self, task_id, machine, spawn_in=None, existing=None, confirm=False,
               engine="claude"):
        """Put a task to work in a session. `engine` picks the CLI for a NEWLY
        spawned session — "claude" (default) or "codex"; it is ignored when you
        reuse `existing` (that session's engine is already what it is). The
        engine is recorded on the task, so the ledger says which CLI did the
        work."""
        engine = _norm_engine(engine)

        def do():
            c = self.clients.get(machine)
            if not c:
                return {"ok": False, "error": f"no such machine: {machine}"}
            t = self.ledger.get(task_id)
            if not t:
                return {"ok": False, "error": f"no such task: {task_id}"}
            cid = existing
            spawned = False
            if not cid:
                if not spawn_in:
                    return {"ok": False, "error": "need spawn_in (a pid) or existing (a cid)"}
                cid = c.new_session(spawn_in, engine=engine)
                if not cid:
                    return {"ok": False, "error": "failed to spawn a session (timeout)"}
                spawned = True
            ran_on = engine if spawned else \
                ((c.state()["sessions"] and next(
                    (s.get("engine") for s in c.state()["sessions"]
                     if s["cid"] == cid), None)) or "claude")
            self.ledger.assign(task_id, cid, machine, engine=ran_on)
            kickoff = t["goal"]
            if t.get("acceptance"):
                kickoff += f"\n\nDone when: {t['acceptance']}"
            c.send_message(cid, kickoff)
            return {"ok": True, "task": task_id, "machine": machine,
                    "cid": cid, "spawned": spawned, "engine": ran_on,
                    "kickoff": kickoff}
        return self._gate("assign", {"task_id": task_id, "machine": machine,
                                     "spawn_in": spawn_in, "existing": existing,
                                     "engine": engine, "confirm": confirm}, do)

    def answer_prompt(self, machine, cid, keys, confirm=False):
        """Clear a `waiting` session parked on a TUI menu by sending raw keys
        (e.g. "1\\r" to pick option 1, "\\r" to accept the default, "\\x1b[B\\r"
        for down-then-enter). The one verb that leaks the keystroke layer — a
        waiting session is a menu, not a text box. Inspect blocked_on first."""
        def do():
            c = self.clients.get(machine)
            if not c:
                return {"ok": False, "error": f"no such machine: {machine}"}
            return {"ok": c.raw_input(cid, keys), "machine": machine, "cid": cid, "keys": keys}
        return self._gate("answer_prompt", {"machine": machine, "cid": cid,
                                            "keys": keys, "confirm": confirm}, do)

    def interrupt(self, machine, cid, confirm=False):
        def do():
            c = self.clients.get(machine)
            if not c:
                return {"ok": False, "error": f"no such machine: {machine}"}
            return {"ok": c.raw_input(cid, "\x1b"), "machine": machine, "cid": cid}
        return self._gate("interrupt", {"machine": machine, "cid": cid,
                                        "confirm": confirm}, do)

    def create_project(self, machine, name, confirm=False):
        def do():
            c = self.clients.get(machine)
            if not c:
                return {"ok": False, "error": f"no such machine: {machine}"}
            return {"ok": c.create_project(name), "machine": machine, "name": name}
        return self._gate("create_project", {"machine": machine, "name": name,
                                             "confirm": confirm}, do)

    def clone_project(self, machine, repo_url, confirm=False):
        def do():
            c = self.clients.get(machine)
            if not c:
                return {"ok": False, "error": f"no such machine: {machine}"}
            return {"ok": c.add_project(repo_url), "machine": machine, "repo": repo_url}
        return self._gate("clone_project", {"machine": machine, "repo_url": repo_url,
                                            "confirm": confirm}, do)

    def spawn(self, machine, pid, confirm=False, engine="claude"):
        """Start a NEW session in a project (a pid) with no task attached. Returns its
        cid so you can `ask` it next. For task-bound spawning use `assign` instead.
        `engine` picks the agent CLI: "claude" (default) or "codex".

        An unknown engine is normalized to claude HERE as well as server-side:
        the harness would silently fall back anyway, and echoing the requested
        name back would tell the PM a session runs a CLI that it doesn't."""
        engine = _norm_engine(engine)

        def do():
            c = self.clients.get(machine)
            if not c:
                return {"ok": False, "error": f"no such machine: {machine}"}
            cid = c.new_session(pid, engine=engine)
            if not cid:
                return {"ok": False, "error": "failed to spawn a session (timeout)"}
            return {"ok": True, "machine": machine, "pid": pid, "cid": cid,
                    "engine": engine}
        return self._gate("spawn", {"machine": machine, "pid": pid,
                                    "engine": engine, "confirm": confirm}, do)

    def close(self, machine, cid, confirm=False):
        """Close a session: its `claude` is terminated and dropped from the harness.
        Irreversible — inspect session_digest first. Frees the slot; the project stays."""
        def do():
            c = self.clients.get(machine)
            if not c:
                return {"ok": False, "error": f"no such machine: {machine}"}
            return {"ok": c.close_session(cid), "machine": machine, "cid": cid}
        return self._gate("close", {"machine": machine, "cid": cid,
                                    "confirm": confirm}, do)

    def pin(self, machine, cid, on=True, confirm=False):
        """📌 Park a finished session on the pin board (`on=true`) or bring it
        back to the tab strip (`on=false`). The right move for "the work is done
        but a human still has to go and check it": the session stays alive and
        promptable, leaves the tab strip, and the harness derives its blue
        test-hint line (see get_pins).

        Not free: pinning also fires a `/compact` once the session goes idle
        (parking is when compaction is cheap). Don't pin something mid-thought
        you intend to keep chatting to — pin it when it's parked."""
        def do():
            c = self.clients.get(machine)
            if not c:
                return {"ok": False, "error": f"no such machine: {machine}"}
            return {"ok": c.pin_session(cid, on), "machine": machine, "cid": cid,
                    "pinned": bool(on)}
        return self._gate("pin", {"machine": machine, "cid": cid, "on": bool(on),
                                  "confirm": confirm}, do)

    def add_local_project(self, machine, path, confirm=False):
        """Adopt an EXISTING folder on that machine's disk as a **private local
        project**: sessions run in it like any project, but the harness never
        runs gh/git-remote operations against it and never stores a repo URL.
        Use when the operator names a folder that isn't a clawdbotatg repo.
        The path only ever leaves the machine inside encrypted fleet frames."""
        def do():
            c = self.clients.get(machine)
            if not c:
                return {"ok": False, "error": f"no such machine: {machine}"}
            return {"ok": c.add_local_project(path), "machine": machine,
                    "path": path,
                    "note": "the harness replies with an `error` frame (not to "
                            "us) if the path is rejected — re-read get_world to "
                            "confirm it appeared"}
        return self._gate("add_local_project", {"machine": machine, "path": path,
                                                "confirm": confirm}, do)

    def remove_project(self, machine, pid, confirm=False):
        """Detach a `kind:"local"` project: drop it from the registry and close
        its sessions. **Never touches the folder on disk.** Silently ignored for
        gh projects (those are removed by deleting the folder, which is not
        something this controller can or should do) and for the pinned self
        project."""
        def do():
            c = self.clients.get(machine)
            if not c:
                return {"ok": False, "error": f"no such machine: {machine}"}
            proj = next((p for p in c.state()["projects"]
                         if p.get("pid") == pid), None)
            if not proj:
                return {"ok": False, "error": f"no such project: {pid}"}
            if proj.get("kind") != "local":
                return {"ok": False, "error": f"{pid} is a {proj.get('kind') or 'gh'} "
                        "project — only local projects can be detached; a gh "
                        "project goes away when its folder is deleted on the box"}
            return {"ok": c.remove_project(pid), "machine": machine, "pid": pid,
                    "name": proj.get("name"), "folder_kept": True}
        return self._gate("remove_project", {"machine": machine, "pid": pid,
                                             "confirm": confirm}, do)


def _clean(args):
    return {k: v for k, v in args.items() if k != "confirm"}


def engine_tag(s):
    """`{"engine": "codex"}` for a non-claude session, `{}` otherwise.

    Emitted only when it isn't claude, deliberately: these dicts ride in
    every find/sweep/world reply, so spending a field on the default on every
    row would cost real budget — and "absent means claude" is the same rule the
    wire protocol already uses for pre-engine harnesses. Documented in the tool
    descriptions so the reading model can't misread silence as "unknown"."""
    eng = (s or {}).get("engine") or "claude"
    return {} if eng == "claude" else {"engine": eng}


def _norm_engine(engine):
    engine = (engine or "claude").strip().lower()
    return engine if engine in ENGINES else "claude"


def _find_step(task, n):
    return next((s for s in (task.get("steps") or []) if s.get("n") == n), None)


def out_hash(text):
    """Short fingerprint of a step's answer. Used to tell "this session has
    said something NEW" from "this is the answer that was already sitting
    there" — the difference between advancing a pipeline and skipping a step."""
    text = (text or "").strip()
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12] if text else ""


def _norm_steps(steps):
    """Validate + normalize create_pipeline's `steps` → (steps, error).

    Strict on purpose: a pipeline runs unattended once approved, so a
    malformed plan has to fail at creation time (where the PM can see the
    message and fix it) rather than three steps in."""
    if not isinstance(steps, (list, tuple)) or not steps:
        return None, ("steps must be a non-empty list of "
                      "{role, engine, prompt} objects")
    if len(steps) > PIPE_MAX_STEPS:
        return None, (f"too many steps ({len(steps)} > {PIPE_MAX_STEPS}) — a "
                      "pipeline is a short chain; split the work into tasks")
    out = []
    for i, raw in enumerate(steps, 1):
        if isinstance(raw, str):                  # a bare string = just a prompt
            raw = {"prompt": raw}
        if not isinstance(raw, dict):
            return None, f"step {i} is not an object"
        prompt = (raw.get("prompt") or "").strip()
        if not prompt:
            return None, f"step {i} has no prompt"
        reuse = raw.get("reuse")
        if reuse not in (None, ""):
            try:
                reuse = int(reuse)
            except (TypeError, ValueError):
                return None, f"step {i}: reuse must be an earlier step number"
            if not 1 <= reuse < i:
                return None, (f"step {i}: reuse={reuse} must name an EARLIER "
                              f"step (1..{i - 1})")
        else:
            reuse = None
        out.append({"n": i,
                    "role": str(raw.get("role") or f"step{i}")[:40],
                    "engine": _norm_engine(raw.get("engine")),
                    "prompt": prompt[:4_000],
                    "pid": raw.get("pid") or None,
                    "machine": raw.get("machine") or None,
                    "reuse": reuse,
                    "status": "pending", "cid": None, "output": ""})
    return out, None
