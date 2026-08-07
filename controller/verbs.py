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
import json
import threading
import time
import urllib.parse

WRITE_VERBS = {"assign", "ask", "answer_prompt", "interrupt",
               "create_project", "clone_project", "spawn", "close"}

# Belt-and-braces ceiling for read-verb replies: the brain's tool-output budget
# is ~25k tokens; anything we hand it must stay comfortably under that even if
# the world grows 10x. get_world degrades (drop digests → counts-only) rather
# than ever exceeding it.
WORLD_CHAR_BUDGET = 20_000
FIND_LIMIT_MAX = 40
FIND_FANOUT_BUDGET_S = 12.0


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
                                   ("lastAnswer", s.get("lastAnswer"))):
                    if val and ql in val.lower():
                        add({"where": where, "machine": mid, "cid": s["cid"],
                             "pid": s.get("pid"),
                             "title": (s.get("title") or s["cid"])[:60],
                             "snippet": val[:160],
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
                for hit in r.get("matches", []):
                    add({"where": "transcript", "machine": mid,
                         "cid": hit.get("cid"), "pid": hit.get("pid"),
                         "title": (hit.get("title") or "")[:60],
                         "snippet": (hit.get("snippet") or "")[:160],
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
        suggested clearing verb — plus rollups (idle sessions with no task,
        in-progress tasks whose sessions are gone). Read-only: acting on it
        stays with the persona + the autonomy gate."""
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
        idle_no_task, stale_tasks = [], []
        live_cids = set()
        for mid, c in self.clients.items():
            for s in c.state()["sessions"]:
                live_cids.add(s["cid"])
                if (self.world._status(s) == "idle" and not s.get("pinned")
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
                "idle_no_task": idle_no_task[:15],
                "stale_tasks": stale_tasks[:10]}

    def list_tasks(self, status=None):
        tasks = self.ledger.list_tasks(status)[:50]
        for t in tasks:                       # full history stays in get_task
            if len(t.get("history", [])) > 3:
                t["history"] = t["history"][-3:]
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

    def get_task(self, task_id):
        return self.ledger.get(task_id) or {"error": f"no such task: {task_id}"}

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

    # ── navigate: "send me to" a session/project in the harness UI ────────────
    # Read-only — these build a deep link, they don't touch the fleet. The chat
    # UI renders any result carrying `nav:true` as an "Open ↗" button (and the
    # url is in the reply text too, so Telegram/non-browser clients still get it).
    def _harness_link(self, pid=None, cid=None, view="transcript", machine=None):
        """Build the deep link into the harness UI. Hash route mirrors index.html:
        direct mode  `#/p/<pid>/s/<cid>` ; fleet/box mode is machine-prefixed —
        `#/m/<machine>/p/<pid>/s/<cid>` (`…/tty` for the terminal). Returns an
        absolute `url` plus a host-relative `path` (+ `port`) so the browser can
        rebuild it against its own origin — see pmNavHref()/navHref() in the UI.

        Fleet mode (CONTROLLER_RELAY set): the UI is served by the public relay at
        its own origin under a passkey, so the link drops the harness `?t=` token
        and the box-internal :8788 port (`port=None` → the browser rebuilds against
        the public origin it's already viewing). Direct mode keeps the token+port."""
        from . import config
        seg = ""
        if config.fleet_mode() and machine:
            seg = "m/" + urllib.parse.quote(machine) + "/"
        if cid and pid:
            frag = f"#/{seg}p/{pid}/s/{cid}" + ("/tty" if view == "tty" else "")
        elif pid:
            frag = f"#/{seg}p/{pid}"
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

    def assign(self, task_id, machine, spawn_in=None, existing=None, confirm=False):
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
                cid = c.new_session(spawn_in)
                if not cid:
                    return {"ok": False, "error": "failed to spawn a session (timeout)"}
                spawned = True
            self.ledger.assign(task_id, cid, machine)
            kickoff = t["goal"]
            if t.get("acceptance"):
                kickoff += f"\n\nDone when: {t['acceptance']}"
            c.send_message(cid, kickoff)
            return {"ok": True, "task": task_id, "machine": machine,
                    "cid": cid, "spawned": spawned, "kickoff": kickoff}
        return self._gate("assign", {"task_id": task_id, "machine": machine,
                                     "spawn_in": spawn_in, "existing": existing,
                                     "confirm": confirm}, do)

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

    def spawn(self, machine, pid, confirm=False):
        """Start a NEW session in a project (a pid) with no task attached. Returns its
        cid so you can `ask` it next. For task-bound spawning use `assign` instead."""
        def do():
            c = self.clients.get(machine)
            if not c:
                return {"ok": False, "error": f"no such machine: {machine}"}
            cid = c.new_session(pid)
            if not cid:
                return {"ok": False, "error": "failed to spawn a session (timeout)"}
            return {"ok": True, "machine": machine, "pid": pid, "cid": cid}
        return self._gate("spawn", {"machine": machine, "pid": pid,
                                    "confirm": confirm}, do)

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


def _clean(args):
    return {k: v for k, v in args.items() if k != "confirm"}
