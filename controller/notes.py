"""The PM's durable memory — standing priorities + scoped fleet notes.

A middle manager remembers: which project matters this week, which machine is
flaky, what the operator said to leave alone. Engine auto-memory is
deliberately off (it bled facts across threads, keyed on cwd); this is the
legible replacement — a tiny curated store the PM itself writes through verbs
(`remember_note` / `forget_note` / `set_priorities`) and that is rendered into
EVERY turn's system prompt, bounded, so each thread starts knowing what
yesterday's threads learned.

Scopes are free-form but conventional: "machine:clawd-head",
"project:slop-computer-live", "task:T-3", "general". One JSON file
(.clawd-controller.notes.json), atomic replace, no concurrency guard needed
beyond a lock (all writers are in the serve process).
"""
import json
import os
import threading
import time

MAX_PER_SCOPE = 20          # oldest dropped beyond this
MAX_NOTE_CHARS = 300
MAX_PRIORITIES = 10
RENDER_BUDGET = 1800        # cap on what reaches the system prompt


class NotesStore:
    def __init__(self, path=None):
        self.path = path
        self.lock = threading.RLock()
        self.priorities = []     # ordered list of strings (operator intent)
        self.notes = {}          # scope -> [{"t": epoch, "text": str}]
        self._load()

    # -- persistence -----------------------------------------------------------
    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path) as f:
                data = json.load(f)
            self.priorities = [str(p)[:MAX_NOTE_CHARS]
                               for p in data.get("priorities", [])][:MAX_PRIORITIES]
            notes = data.get("notes", {})
            if isinstance(notes, dict):
                self.notes = {str(k): [n for n in v if isinstance(n, dict)][-MAX_PER_SCOPE:]
                              for k, v in notes.items() if isinstance(v, list)}
        except (OSError, ValueError):
            pass

    def _persist(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"priorities": self.priorities, "notes": self.notes}, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # -- writes ------------------------------------------------------------------
    def remember(self, scope, text):
        scope = (scope or "general").strip()[:80]
        text = (text or "").strip()[:MAX_NOTE_CHARS]
        if not text:
            return {"ok": False, "error": "empty note"}
        with self.lock:
            bucket = self.notes.setdefault(scope, [])
            bucket.append({"t": time.time(), "text": text})
            del bucket[:-MAX_PER_SCOPE]
            self._persist()
        return {"ok": True, "scope": scope, "count": len(bucket)}

    def forget(self, scope, index):
        with self.lock:
            bucket = self.notes.get(scope)
            if not bucket:
                return {"ok": False, "error": f"no notes for scope: {scope}"}
            try:
                dropped = bucket.pop(int(index))
            except (ValueError, IndexError):
                return {"ok": False, "error": f"no note {index} in {scope}"}
            if not bucket:
                self.notes.pop(scope, None)
            self._persist()
        return {"ok": True, "scope": scope, "dropped": dropped["text"]}

    def set_priorities(self, priorities):
        if not isinstance(priorities, list):
            return {"ok": False, "error": "priorities must be a list of strings"}
        with self.lock:
            self.priorities = [str(p).strip()[:MAX_NOTE_CHARS]
                               for p in priorities if str(p).strip()][:MAX_PRIORITIES]
            self._persist()
        return {"ok": True, "priorities": list(self.priorities)}

    # -- reads -------------------------------------------------------------------
    def dump(self):
        with self.lock:
            return {"priorities": list(self.priorities),
                    "notes": {k: [dict(n, i=i) for i, n in enumerate(v)]
                              for k, v in self.notes.items()}}

    def render(self, budget=RENDER_BUDGET):
        """The bounded system-prompt block. Newest notes win the budget."""
        with self.lock:
            parts = []
            if self.priorities:
                parts.append("# Standing priorities (operator-set, highest first)")
                parts += [f"{i + 1}. {p}" for i, p in enumerate(self.priorities)]
            rows = [(n["t"], scope, n["text"])
                    for scope, bucket in self.notes.items() for n in bucket]
        if rows:
            rows.sort(reverse=True)               # newest first into the budget
            lines = []
            used = sum(len(p) for p in parts)
            for _t, scope, text in rows:
                line = f"- [{scope}] {text}"
                if used + len(line) > budget:
                    break
                lines.append(line)
                used += len(line)
            if lines:
                parts.append("# Fleet notes (your own memory — newest first)")
                parts += lines
        return ("\n".join(parts)).strip()
