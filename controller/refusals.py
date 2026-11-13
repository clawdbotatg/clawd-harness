"""Hearing a harness "no".

A write frame (wrap, check, add a project) gets no reply when it works. When
the harness refuses, it answers with an `error` frame — which the PM used to
drop, so a refused verb still said ok. `Refusals` lets a client send a frame
and wait a moment for a matching `error` before answering.

There is no "accepted" frame, so success costs the full wait. Keep it short.
"""
import threading


class Refusals:
    def __init__(self):
        self._lock = threading.Lock()
        self._waiters = []

    def feed(self, frame):
        """Call for every inbound frame; wakes the waiters it answers."""
        with self._lock:
            waiters = list(self._waiters)
        for w in waiters:
            try:
                why = w["match"](frame)
            except Exception:
                why = None
            if why:
                w["why"] = why
                w["ev"].set()

    def ask(self, send, match, wait):
        """Run `send()`; return the refusal text, or None if none came within
        `wait` seconds. `match(frame)` returns the reason for a frame that
        refuses THIS request, else a falsy value. Registered before the send,
        so a fast "no" can't slip past."""
        w = {"match": match, "ev": threading.Event(), "why": None}
        with self._lock:
            self._waiters.append(w)
        try:
            if not send():
                return "not connected"
            w["ev"].wait(wait)
            return w["why"]
        finally:
            with self._lock:
                self._waiters.remove(w)


def for_cid(cid):
    """An `error` frame about this session."""
    return lambda f: (f.get("type") == "error" and f.get("cid") == cid
                      and (f.get("error") or "refused"))


def with_prefix(prefix, missing_type=None):
    """A cid-less `error` frame whose text starts with `prefix` (project
    adds answer "addLocalProject: …"). `missing_type` also counts the
    harness's "that folder doesn't exist" frame as a no."""
    def match(f):
        t = f.get("type")
        if t == "error" and str(f.get("error") or "").startswith(prefix):
            return f["error"]
        if missing_type and t == missing_type:
            return f"no such folder: {f.get('path') or '?'}"
        return None
    return match
