"""The task ledger — the controller's intent layer.

The harness owns *execution* (sessions, PTYs, transcripts) and knows nothing
about "tasks". The controller owns *intent*: what each session was asked to
accomplish, and how that's tracked over time. That state lives here.

No database (the whole stack is proud of being pure stdlib). It's an
**append-only JSONL event log** (`.clawd-controller.tasks.jsonl`): the log *is*
the history, folded into in-memory state on load. One file gives current state
(replay), the audit trail (every write verb appends an `action` event), and
time-travel (grep). Append-only also dodges mid-write corruption of a rewritten
doc. See docs/CONTROLLER.md.
"""
import json
import threading
import time

# Task lifecycle: open (created, unassigned) → in_progress (assigned to a
# session) → blocked | review | done. Free-form beyond these is allowed; these
# are what the brain/attention logic key off.
STATUSES = ("open", "in_progress", "blocked", "review", "done", "cancelled")


class TaskLedger:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.tasks = {}          # id -> task dict
        self.actions = []        # recent audit events (also persisted in the log)
        self._seq = 0
        self._load()

    # -- event sourcing --------------------------------------------------------
    def _load(self):
        try:
            with open(self.path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._apply(json.loads(line))
                    except Exception:
                        continue
        except OSError:
            pass

    def _apply(self, ev):
        """Fold one event into in-memory state (used on load *and* on append)."""
        kind = ev.get("ev")
        t = ev.get("t")
        if kind == "task_created":
            tid = ev["id"]
            self.tasks[tid] = {
                "id": tid, "goal": ev.get("goal", ""),
                "project": ev.get("project"), "machine": ev.get("machine"),
                "acceptance": ev.get("acceptance", ""), "status": "open",
                "sessions": [], "created": t, "updated": t,
                "history": [{"t": t, "event": "created"}],
            }
            self._bump_seq(tid)
        elif kind == "assigned":
            tk = self.tasks.get(ev["id"])
            if tk:
                cid = ev.get("cid")
                if cid and cid not in tk["sessions"]:
                    tk["sessions"].append(cid)
                if ev.get("machine"):
                    tk["machine"] = ev["machine"]
                if tk["status"] == "open":
                    tk["status"] = "in_progress"
                tk["updated"] = t
                tk["history"].append({"t": t, "event": f"assigned → {cid}"})
        elif kind == "status":
            tk = self.tasks.get(ev["id"])
            if tk:
                tk["status"] = ev.get("status")
                tk["updated"] = t
                tk["history"].append({"t": t, "event": f"status → {ev.get('status')}"})
        elif kind == "note":
            tk = self.tasks.get(ev["id"])
            if tk:
                tk["updated"] = t
                tk["history"].append({"t": t, "event": f"note: {ev.get('text', '')}"})
        elif kind == "action":
            self.actions.append(ev)
            if len(self.actions) > 1000:
                self.actions = self.actions[-1000:]

    def _bump_seq(self, tid):
        try:
            self._seq = max(self._seq, int(str(tid).split("-")[-1]))
        except (ValueError, IndexError):
            pass

    def _append(self, ev):
        ev.setdefault("t", time.time())
        with self.lock:
            with open(self.path, "a") as f:
                f.write(json.dumps(ev) + "\n")
            self._apply(ev)
        return ev

    # -- writes ----------------------------------------------------------------
    def new_id(self):
        with self.lock:
            self._seq += 1
            return f"T-{self._seq}"

    def create_task(self, goal, project=None, acceptance="", machine=None):
        tid = self.new_id()
        self._append({"ev": "task_created", "id": tid, "goal": goal,
                      "project": project, "acceptance": acceptance, "machine": machine})
        return self.get(tid)

    def assign(self, tid, cid, machine=None):
        self._append({"ev": "assigned", "id": tid, "cid": cid, "machine": machine})
        return self.get(tid)

    def set_status(self, tid, status):
        self._append({"ev": "status", "id": tid, "status": status})
        return self.get(tid)

    def note(self, tid, text):
        self._append({"ev": "note", "id": tid, "text": text})
        return self.get(tid)

    def audit(self, verb, args, result):
        ok = result.get("ok", True) if isinstance(result, dict) else True
        self._append({"ev": "action", "verb": verb, "args": args, "ok": ok})

    # -- reads -----------------------------------------------------------------
    def get(self, tid):
        with self.lock:
            tk = self.tasks.get(tid)
            return json.loads(json.dumps(tk)) if tk else None

    def list_tasks(self, status=None):
        with self.lock:
            tasks = list(self.tasks.values())
        if status:
            tasks = [t for t in tasks if t.get("status") == status]
        tasks.sort(key=lambda t: t.get("updated") or 0, reverse=True)
        return json.loads(json.dumps(tasks))

    def task_for_cid(self, cid):
        with self.lock:
            for t in self.tasks.values():
                if cid in t.get("sessions", []) and t.get("status") != "done":
                    return t["id"]
        return None

    def recent_actions(self, n=20):
        with self.lock:
            return list(self.actions[-n:])
