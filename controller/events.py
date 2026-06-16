"""The Reactor — fleet hooks → higher-level controller events.

This is the answer to "could a Claude Code hook cause a higher-level hook?": yes.
Each session's low-level hooks (Stop / Notification / PreToolUse) already fan out
to every WS client as `hook` frames. The Reactor watches that stream across all
sessions and detects *transitions* worth a human's attention — a session crossing
into `blocked`, a turn finishing, a session ending — then dispatches a single
higher-level event to registered handlers (a Telegram push, the UI notification
feed, …).

Edge-triggered + deduped: "blocked" fires only on the rising edge (not every hook
while parked), so handlers don't get spammed. See docs/CONTROLLER.md (ambient
triage).
"""
import threading
import time


class Reactor:
    def __init__(self, ledger):
        self.ledger = ledger
        self.handlers = []                 # callables(event_dict)
        self.notifications = []            # ring of recent higher-level events
        self._last = {}                    # (machine,cid) -> {"waiting","busy"}
        self.lock = threading.RLock()

    def on_event(self, fn):
        """Register a handler. Called with the event dict for every higher-level
        event (handler decides whether to act — e.g. only push `blocked`)."""
        self.handlers.append(fn)

    def feed(self, machine, frame):
        """Consume one WS frame (called from HarnessClient). Only `hook` frames
        produce events. Idempotent on non-transitions."""
        if frame.get("type") != "hook":
            return
        cid = frame.get("cid")
        ev = frame.get("event")
        busy = bool(frame.get("busy"))
        waiting = bool(frame.get("waiting"))
        key = (machine, cid)
        with self.lock:
            prev = self._last.get(key, {"waiting": False, "busy": False})
            self._last[key] = {"waiting": waiting, "busy": busy}

        events = []
        if waiting and not prev["waiting"]:           # rising edge into blocked
            msg = (frame.get("data") or {}).get("message") or "needs your input"
            events.append({"kind": "blocked", "machine": machine, "cid": cid,
                           "summary": msg})
        if ev == "Stop":
            tid = self.ledger.task_for_cid(cid)
            last = ((frame.get("data") or {}).get("last") or "").strip()[:240]
            events.append({"kind": "turn_done", "machine": machine, "cid": cid,
                           "task": tid, "summary": last or "turn complete"})
        if ev == "SessionEnd":
            events.append({"kind": "ended", "machine": machine, "cid": cid,
                           "summary": "session ended"})

        for e in events:
            e["t"] = time.time()
            with self.lock:
                self.notifications.append(e)
                if len(self.notifications) > 300:
                    self.notifications = self.notifications[-300:]
            for h in self.handlers:
                try:
                    h(e)
                except Exception:
                    pass

    def recent(self, n=30):
        with self.lock:
            return list(self.notifications[-n:])
