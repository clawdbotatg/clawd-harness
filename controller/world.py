"""The semantic world-model — the read shape an AI PM actually wants.

Aggregates one-or-more HarnessClients (one per machine) into a single snapshot:
machines → projects → sessions, each session carrying the reading-phase meta
(status / digest / blocked_on), its last answer, idle time, and a link to the
task it's executing. Plus the derived **attention queue**: the ranked "needs a
human" list, each item naming the suggested verb to clear it.

Designed to be read by a model in one cheap gulp — no transcript, no ANSI, just
reduced state. See docs/CONTROLLER.md (the reading phase).
"""
import time

_SEV_ORDER = {"high": 0, "medium": 1, "low": 2}


class World:
    def __init__(self, clients, ledger):
        self.clients = clients      # {machine_id: HarnessClient}
        self.ledger = ledger

    # -- the full snapshot -----------------------------------------------------
    def snapshot(self):
        now = time.time()
        machines = []
        for mid, client in self.clients.items():
            st = client.state()
            sess_by_pid = {}
            for s in st["sessions"]:
                s = dict(s)
                cid = s["cid"]
                s["task"] = self.ledger.task_for_cid(cid)
                la = s.get("lastActive") or 0
                s["idle_for_s"] = round(now - la, 1) if la else None
                ans = st["last_answer"].get(cid)
                if ans:
                    s["last_answer"] = ans[:500]
                sess_by_pid.setdefault(s.get("pid"), []).append(s)
            projects = []
            for p in st["projects"]:
                p = dict(p)
                p["sessions"] = sess_by_pid.get(p["pid"], [])
                projects.append(p)
            machines.append({
                "id": mid, "connected": st["connected"],
                "session_total": len(st["sessions"]),
                "projects": projects,
            })
        return {"machines": machines, "generated": now,
                "attention_count": len(self.attention())}

    # -- the derived "needs you" queue ----------------------------------------
    def attention(self, stall_after=900):
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
        return items

    def _item(self, sev, mid, s, kind, summary, action):
        return {
            "sev": sev, "machine": mid, "pid": s.get("pid"), "cid": s["cid"],
            "title": s.get("title") or s["cid"], "kind": kind, "summary": summary,
            "digest": s.get("digest") or "", "blocked_on": s.get("blocked_on") or "",
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
        ans = st["last_answer"].get(cid)
        if ans:
            out["last_answer"] = ans
        return out
