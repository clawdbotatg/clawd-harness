"""The semantic world-model — the read shape an AI PM actually wants.

Aggregates one-or-more HarnessClients (one per machine) into a single snapshot:
machines → projects → sessions. **Compact by default and bounded by
construction**: the full fleet once serialized to 66KB — past the tool-output
budget of the very model that reads it — so the default shape is one line per
session, empty projects collapse to a name list, and per-machine session counts
carry the rest. Scope with `machine=`/`pid=` to drill down; `verbose` (full
session dicts) is honored only when scoped. Plus the derived **attention
queue**: the ranked "needs a human" list, each item naming the suggested verb
to clear it. See docs/CONTROLLER.md (the reading phase).
"""
import time

_SEV_ORDER = {"high": 0, "medium": 1, "low": 2}


class World:
    def __init__(self, clients, ledger):
        self.clients = clients      # {machine_id: HarnessClient}
        self.ledger = ledger

    # -- the full snapshot -----------------------------------------------------
    def snapshot(self, machine=None, pid=None, verbose=False, max_sessions=40):
        now = time.time()
        verbose = bool(verbose) and bool(machine or pid)   # verbose only when scoped
        machines = []
        for mid, client in self.clients.items():
            if machine and mid != machine:
                continue
            st = client.state()
            sessions = st["sessions"]
            if pid:
                sessions = [s for s in sessions if s.get("pid") == pid]
            counts = {"blocked": 0, "working": 0, "background": 0, "idle": 0}
            for s in sessions:
                counts[self._status(s)] = counts.get(self._status(s), 0) + 1
            ordered = sorted(sessions, key=lambda s: s.get("lastActive") or 0,
                             reverse=True)
            dropped = max(0, len(ordered) - max_sessions)
            ordered = ordered[:max_sessions]
            sess_by_pid = {}
            for s in ordered:
                row = (self._verbose_session(s, st, now) if verbose
                       else self._compact_session(s, now))
                sess_by_pid.setdefault(s.get("pid"), []).append(row)
            projects, empty = [], []
            for p in st["projects"]:
                if pid and p.get("pid") != pid:
                    continue
                rows = sess_by_pid.get(p["pid"], [])
                if rows:
                    projects.append({"pid": p["pid"], "name": p.get("name"),
                                     "sessions": rows})
                elif not pid:
                    empty.append(p.get("name") or p["pid"])
            m = {"id": mid, "connected": st["connected"],
                 "sessions": len(st["sessions"]), **counts, "projects": projects}
            if empty:
                m["empty_projects"] = empty[:30]
            if dropped:
                m["more_sessions"] = dropped
                m["hint"] = f"get_world(machine='{mid}') for the rest"
            machines.append(m)
        return {"machines": machines, "generated": now,
                "attention_count": len(self.attention())}

    @staticmethod
    def _status(s):
        return s.get("status") or ("working" if s.get("busy") else "idle")

    def _compact_session(self, s, now):
        """One line per session — the default get_world shape."""
        out = {"cid": s["cid"],
               "title": (s.get("title") or s["cid"])[:60],
               "status": self._status(s)}
        tid = self.ledger.task_for_cid(s["cid"])
        if tid:
            out["task"] = tid
        la = s.get("lastActive") or 0
        if la:
            out["idle_m"] = round((now - la) / 60, 1)
        if s.get("digest"):
            out["digest"] = s["digest"][:80]
        if s.get("blocked_on"):
            out["blocked_on"] = s["blocked_on"][:80]
        return out

    def _verbose_session(self, s, st, now):
        """Today's full session dict — only for scoped (machine/pid) reads."""
        s = dict(s)
        cid = s["cid"]
        s["task"] = self.ledger.task_for_cid(cid)
        la = s.get("lastActive") or 0
        s["idle_for_s"] = round(now - la, 1) if la else None
        ans = st["last_answer"].get(cid) or s.get("lastAnswer")
        if ans:
            s["last_answer"] = ans[:280]
        s.pop("lastAnswer", None)
        return s

    # -- the derived "needs you" queue ----------------------------------------
    def attention(self, stall_after=900, limit=30):
        now = time.time()
        items = []
        for mid, client in self.clients.items():
            for s in client.state()["sessions"]:
                status = s.get("status")
                blocked_on = (s.get("blocked_on") or "").strip()
                la = s.get("lastActive") or 0
                age = now - la if la else 0
                if status == "blocked" or s.get("waiting"):
                    items.append(self._item("high", mid, s, "blocked",
                                 blocked_on or "blocked on an interactive prompt",
                                 "answer_prompt"))
                elif blocked_on:
                    # soft block — turn ended asking the human in plain text
                    items.append(self._item("high", mid, s, "question",
                                 blocked_on, "ask"))
                elif status == "working" and age > stall_after:
                    items.append(self._item("medium", mid, s, "stalled",
                                 f"working {int(age)}s with no new turn", "session_digest"))
                elif status == "idle":
                    tid = self.ledger.task_for_cid(s["cid"])
                    tk = self.ledger.tasks.get(tid) if tid else None
                    if tk and tk.get("status") == "in_progress":
                        items.append(self._item("low", mid, s, "review",
                                     f"{tid} session idle — finished? verify vs acceptance",
                                     "session_digest"))
        items.sort(key=lambda i: _SEV_ORDER.get(i["sev"], 3))
        return items[:limit]

    def _item(self, sev, mid, s, kind, summary, action):
        return {
            "sev": sev, "machine": mid, "pid": s.get("pid"), "cid": s["cid"],
            "title": (s.get("title") or s["cid"])[:60], "kind": kind,
            "summary": summary[:120],
            "digest": (s.get("digest") or "")[:80],
            "blocked_on": (s.get("blocked_on") or "")[:120],
            "task": self.ledger.task_for_cid(s["cid"]),
            "suggested_action": action,
        }

    # -- one session, deep ----------------------------------------------------
    def session_detail(self, machine, cid):
        client = self.clients.get(machine)
        if not client:
            return {"error": f"no such machine: {machine}"}
        st = client.state()
        s = next((x for x in st["sessions"] if x["cid"] == cid), None)
        if not s:
            return {"error": f"no such session: {cid}"}
        out = dict(s)
        out["machine"] = machine
        out["task"] = self.ledger.task_for_cid(cid)
        # hook-fed capture first (fuller, 500 chars), then the broadcast
        # lastAnswer field (280, durable across restarts/reconnects)
        ans = st["last_answer"].get(cid) or s.get("lastAnswer")
        if ans:
            out["last_answer"] = ans[:700]
        out.pop("lastAnswer", None)
        return out
