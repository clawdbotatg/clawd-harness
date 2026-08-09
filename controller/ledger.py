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

# A **pipeline** is a task whose work is an ordered list of steps, each run by
# its own session (and possibly its own engine — claude research, codex review,
# claude write-up). It exists because the PM's turn ends when it replies: there
# was no way to say "when session A finishes, feed its answer to session B".
# The steps live here, in the same append-only log, so the state survives
# restarts and any turn can reconstruct exactly where a pipeline got to.
STEP_STATUSES = ("pending", "running", "done", "failed", "skipped")


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
        elif kind == "pipeline_created":
            tid = ev["id"]
            self.tasks[tid] = {
                "id": tid, "goal": ev.get("goal", ""),
                "project": ev.get("project"), "machine": ev.get("machine"),
                "acceptance": ev.get("acceptance", ""), "status": "open",
                "sessions": [], "created": t, "updated": t,
                "pipeline": True, "steps": ev.get("steps") or [],
                "history": [{"t": t, "event":
                             f"pipeline created ({len(ev.get('steps') or [])} steps)"}],
            }
            self._bump_seq(tid)
        elif kind == "assigned":
            tk = self.tasks.get(ev["id"])
            if tk:
                cid = ev.get("cid")
                eng = ev.get("engine")
                if cid and cid not in tk["sessions"]:
                    tk["sessions"].append(cid)
                if cid and eng:
                    # which CLI ran which session — otherwise nothing records
                    # that this task's review step was double-checked by codex
                    tk.setdefault("engines", {})[cid] = eng
                if ev.get("machine"):
                    tk["machine"] = ev["machine"]
                if tk["status"] == "open":
                    tk["status"] = "in_progress"
                tk["updated"] = t
                tk["history"].append({"t": t, "event": "assigned → " + str(cid)
                                      + (f" ({eng})" if eng and eng != "claude" else "")})
        elif kind == "step_started":
            tk = self.tasks.get(ev["id"])
            st = self._step(tk, ev.get("n"))
            if tk and st:
                cid, eng = ev.get("cid"), ev.get("engine")
                st.update(status="running", cid=cid, started=t)
                if ev.get("machine"):
                    st["machine"] = ev["machine"]
                if ev.get("prompt") is not None:
                    st["sent"] = ev["prompt"]
                # fingerprint of whatever answer was already in that session, so
                # a reused session's previous reply can't pass as this step's
                st["base"] = ev.get("base") or ""
                if cid and cid not in tk["sessions"]:
                    tk["sessions"].append(cid)
                if cid and eng:
                    tk.setdefault("engines", {})[cid] = eng
                if tk["status"] == "open":
                    tk["status"] = "in_progress"
                tk["step"] = ev.get("n")
                tk["updated"] = t
                tk["history"].append({"t": t, "event":
                                      f"step {ev.get('n')} ({st.get('role') or '?'}"
                                      f"/{st.get('engine') or 'claude'}) started → {cid}"})
        elif kind == "step_done":
            tk = self.tasks.get(ev["id"])
            st = self._step(tk, ev.get("n"))
            if tk and st:
                status = ev.get("status") or "done"
                st.update(status=status, output=ev.get("output") or "", finished=t)
                tk["updated"] = t
                tk["history"].append({"t": t, "event":
                                      f"step {ev.get('n')} {status}"})
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

    @staticmethod
    def _step(tk, n):
        if not tk:
            return None
        return next((s for s in tk.get("steps") or [] if s.get("n") == n), None)

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

    def assign(self, tid, cid, machine=None, engine=None):
        self._append({"ev": "assigned", "id": tid, "cid": cid, "machine": machine,
                      "engine": engine})
        return self.get(tid)

    # -- pipelines (multi-step, possibly multi-engine tasks) -------------------
    def create_pipeline(self, goal, steps, project=None, acceptance="", machine=None):
        tid = self.new_id()
        self._append({"ev": "pipeline_created", "id": tid, "goal": goal,
                      "project": project, "acceptance": acceptance,
                      "machine": machine, "steps": steps})
        return self.get(tid)

    def start_step(self, tid, n, cid, machine=None, engine=None, prompt=None,
                   base=""):
        self._append({"ev": "step_started", "id": tid, "n": n, "cid": cid,
                      "machine": machine, "engine": engine, "prompt": prompt,
                      "base": base})
        return self.get(tid)

    def finish_step(self, tid, n, output, status="done"):
        self._append({"ev": "step_done", "id": tid, "n": n,
                      "output": output, "status": status})
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

    def pipelines(self, active_only=True):
        """Every pipeline task, newest-touched first. `active_only` keeps the
        ones still worth advancing (a finished/cancelled pipeline never is)."""
        out = []
        for t in self.list_tasks():
            if not t.get("pipeline"):
                continue
            if active_only and t.get("status") in ("done", "cancelled", "review"):
                continue
            out.append(t)
        return out

    def running_step_for_cid(self, cid):
        """(task, step) if `cid` is the session currently running a pipeline
        step — the hook the autopilot needs to know "that Stop just finished
        step 2 of T-7", as opposed to any old task-linked session."""
        for t in self.pipelines():
            for s in t.get("steps") or []:
                if s.get("cid") == cid and s.get("status") == "running":
                    return t, s
        return None, None

    def recent_actions(self, n=20):
        with self.lock:
            return list(self.actions[-n:])
