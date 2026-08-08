"""Multiple PM conversation threads — the chat analog of per-project sessions.

The harness keeps N independent `claude` sessions per project so you can run
several lines of work at once, switch between them, and drop the ones you're done
with. This is the same idea for the *PM chat*: each **thread** is an isolated
dialog with its own brain state (history + the `--resume` session id, so
continuity is preserved across turns) plus a display transcript for the UI. You
spawn threads, switch between them, **clear** one (wipe its context but keep the
slot) or **archive** one (hide it; restorable). This is how you keep a long PM
session's context from bleeding across unrelated topics.

Persisted to a JSON file (like `.clawd-harness.sessions.json`) so threads — and
the brain state riding inside them — survive a daemon restart, carrying each
thread's `--resume` id across too.

**Thread bookkeeping is deliberately NOT serialized with brain turns.** A turn
takes minutes; the chat server's turn lock must never be what a click on ＋ new /
a thread tab / ✕ archive waits on, or the whole PM tab goes dead for the length
of the turn (it did — every one of those endpoints sat on the chat lock). So this
class owns its own short-held `RLock` instead: mutations and `persist()` run
under it, and metadata ops stay safe while a turn is in flight. Turns pin their
`tid` up front (see `Router.chat`), so `current` moving mid-turn can't misroute a
transcript.
"""
import json
import os
import threading

_TITLE_MAX = 48


def _derive_title(text):
    """A short title from the first user message (first line, trimmed)."""
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    line = " ".join(line.split())
    if len(line) > _TITLE_MAX:
        line = line[:_TITLE_MAX - 1].rstrip() + "…"
    return line or "New thread"


class Threads:
    """Owns the set of PM threads + which one is current. A thread is a plain
    dict: {id, title, desc, title_locked, archived, messages, state}. `state` maps a
    brain backend name → that brain's exported conversation (history etc.)."""

    def __init__(self, path=None):
        self.path = path
        # Reentrant: archive() → new() → persist() all want it, and every public
        # method takes it. Held for microseconds — never across a brain turn.
        self._lock = threading.RLock()
        self.threads = {}        # tid -> thread dict
        self.order = []          # tids, creation order
        self.current = None
        self._seq = 0
        self._load()
        if not self.live():
            self.new()           # always have one live thread to talk to

    # -- queries ---------------------------------------------------------------
    def live(self):
        with self._lock:
            return [t for t in self.order if not self.threads[t]["archived"]]

    def get(self, tid):
        return self.threads.get(tid)

    def current_thread(self):
        return self.threads.get(self.current)

    def summary(self):
        """UI-facing list: live threads first (creation order), then archived."""
        def row(tid):
            t = self.threads[tid]
            return {"id": tid, "title": t["title"], "desc": t.get("desc", ""),
                    "archived": t["archived"],
                    "count": sum(1 for m in t["messages"] if m["who"] == "me"),
                    "msgs": len(t["messages"]),   # total — lets the UI spot a turn that landed while it wasn't looking
                    "current": tid == self.current}
        with self._lock:
            live = [row(t) for t in self.order if not self.threads[t]["archived"]]
            arch = [row(t) for t in self.order if self.threads[t]["archived"]]
            return {"threads": live + arch, "current": self.current,
                    "archived_count": len(arch)}

    def messages(self, tid=None):
        with self._lock:
            t = self.threads.get(tid or self.current)
            return list(t["messages"]) if t else []

    # -- brain state in/out ----------------------------------------------------
    def state_for(self, backend, tid=None):
        with self._lock:
            t = self.threads.get(tid or self.current)
            return (t["state"].get(backend) if t else None) or {}

    def save_state(self, backend, state, tid=None):
        with self._lock:
            t = self.threads.get(tid or self.current)
            if t is not None:
                t["state"][backend] = state

    def record(self, who, text, trace=None, tid=None):
        """Append a display message; lock the title from the first user message."""
        with self._lock:
            t = self.threads.get(tid or self.current)
            if t is None:
                return
            t["messages"].append({"who": who, "text": text, "trace": trace or []})
            if who == "me" and not t["title_locked"]:
                t["title"] = _derive_title(text)
                t["title_locked"] = True

    def set_name(self, tid, title=None, desc=None):
        """AI naming (controller/naming.py) refreshing a thread's title + running
        tldr. Overrides the first-prompt title (and locks it so a later user
        message can't re-derive over the AI's name)."""
        with self._lock:
            t = self.threads.get(tid)
            if t is None:
                return False
            if title:
                t["title"] = title[:_TITLE_MAX]
                t["title_locked"] = True
            if desc is not None:
                t["desc"] = desc
            self.persist()
            return True

    # -- mutations -------------------------------------------------------------
    def new(self, title=None, select=True):
        with self._lock:
            self._seq += 1
            tid = f"t{self._seq}"
            self.threads[tid] = {"id": tid, "title": title or "New thread",
                                 "desc": "",
                                 "title_locked": bool(title), "archived": False,
                                 "messages": [], "state": {}}
            self.order.append(tid)
            if select or self.current is None:
                self.current = tid
            self.persist()
            return tid

    def select(self, tid):
        with self._lock:
            t = self.threads.get(tid)
            if t is None:
                return False
            if t["archived"]:           # selecting an archived thread restores it
                t["archived"] = False
            self.current = tid
            self.persist()
            return True

    def clear(self, tid=None):
        """Wipe a thread's context (history + transcript) but keep the slot."""
        with self._lock:
            t = self.threads.get(tid or self.current)
            if t is None:
                return False
            t["messages"] = []
            t["state"] = {}
            t["title"] = "New thread"
            t["desc"] = ""
            t["title_locked"] = False
            self.persist()
            return True

    def archive(self, tid=None):
        """Hide a thread. If it was current, move to another live thread (or make
        a fresh one so there's always something to talk to)."""
        with self._lock:
            t = self.threads.get(tid or self.current)
            if t is None:
                return False
            t["archived"] = True
            if self.current == t["id"]:
                others = self.live()
                self.current = others[0] if others else self.new(select=True)
            self.persist()
            return True

    # -- persistence -----------------------------------------------------------
    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for t in data.get("threads", []):
            tid = t.get("id")
            if not tid:
                continue
            self.threads[tid] = {
                "id": tid, "title": t.get("title") or "New thread",
                "desc": t.get("desc") or "",
                "title_locked": bool(t.get("title_locked")),
                "archived": bool(t.get("archived")),
                "messages": t.get("messages") or [],
                "state": t.get("state") or {}}
            self.order.append(tid)
        self._seq = int(data.get("seq", 0) or 0)
        cur = data.get("current")
        self.current = cur if cur in self.threads else (self.live()[0] if self.live() else None)
        # keep the id counter ahead of anything on disk (defensive)
        for tid in self.threads:
            if tid.startswith("t") and tid[1:].isdigit():
                self._seq = max(self._seq, int(tid[1:]))

    def persist(self):
        if not self.path:
            return
        # Under the lock: `tmp` is a single fixed path, so two concurrent
        # persists (a turn recording its reply while you click ＋ new) would
        # interleave writes into the same file and rename a half-written blob
        # over the real one. Serializing the JSON here also snapshots a
        # consistent view of threads/order/current.
        with self._lock:
            data = {"seq": self._seq, "current": self.current,
                    "threads": [self.threads[t] for t in self.order]}
            blob = json.dumps(data)
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w") as f:
                    f.write(blob)
                os.replace(tmp, self.path)
            except OSError:
                pass
