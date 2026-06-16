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
import time

WRITE_VERBS = {"assign", "ask", "answer_prompt", "interrupt",
               "create_project", "clone_project"}


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
    def __init__(self, world, ledger, clients, guard):
        self.world = world
        self.ledger = ledger
        self.clients = clients
        self.guard = guard

    # ── read ──────────────────────────────────────────────────────────────
    def get_world(self):
        return self.world.snapshot()

    def get_attention(self):
        return {"items": self.world.attention()}

    def session_digest(self, machine, cid):
        return self.world.session_detail(machine, cid)

    def list_tasks(self, status=None):
        return {"tasks": self.ledger.list_tasks(status)}

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


def _clean(args):
    return {k: v for k, v in args.items() if k != "confirm"}
