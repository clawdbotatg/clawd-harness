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
import urllib.parse

WRITE_VERBS = {"assign", "ask", "answer_prompt", "interrupt",
               "create_project", "clone_project", "spawn", "close"}


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
