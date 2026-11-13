#!/usr/bin/env python3
"""☰ session task list (server.py): one list per session, written by the
session with `bin/harness-tasks`, shown as the chip beside the terminal.

  1. tasks_op: add (high default / low), duplicate + empty refused, done /
     undone / rm by id or a few words, lev, clear, the item cap.
  2. A session's tasks_apply saves + broadcasts on a change only, and its
     tasks_meta carries the counts (open high first, then low, then done).
  3. self_tasks (what `harness-tasks` hits): list / add / done lines, a
     refused op is 409, an unknown session 404.
  4. Durable: to_registry → ctor round-trip, and clone_for_respawn keeps it.
  5. tasks_note, the only thing a hook ever says back to claude: the open
     items after a compact/resume SessionStart (never a fresh startup); ONE
     reminder once the list sat untouched for TASKS_STALE_TURNS prompts, then
     quiet again; a touch resets it; nothing for an empty or all-done list,
     a reviewer, a ceremony, or at Stop.
  6. The rule rides --append-system-prompt for a working claude session, and
     the hook settings print /hook's reply only for the two context events.

Run: python3 test_tasks.py
"""
import json
import sys
import types

import server

FAILS = []


def check(name, ok, detail=""):
    print(f"  {'✓' if ok else '✗ FAIL'} {name}" + ("" if ok or not detail else f" — {detail}"))
    if not ok:
        FAILS.append(name)


class Mgr:
    def __init__(self):
        self.saved = self.casts = 0
        self.sessions, self.projects, self.accounts = {}, {}, {}

    def save_registry(self):
        self.saved += 1

    def broadcast_sessions(self):
        self.casts += 1

    def get(self, cid):
        return self.sessions.get(cid)


def sess(mgr, cid="c1", **kw):
    s = server.ClaudeSession(mgr, cid=cid, session_id="sid-" + cid, resuming=False, **kw)
    mgr.sessions[cid] = s
    return s


print("1. tasks_op")
items = []
ok, msg = server.tasks_op(items, "add", text="ship the chip")
check("add defaults to high", ok and items[0]["lev"] == "hi" and "(high)" in msg, msg)
ok, msg = server.tasks_op(items, "add", text="tidy the css", lev="low")
check("add --low", ok and items[1]["lev"] == "lo", msg)
check("duplicate open text refused", not server.tasks_op(items, "add", text="Ship the chip")[0])
check("empty text refused", not server.tasks_op(items, "add", text="   ")[0])
ok, msg = server.tasks_op(items, "done", ref="tidy")
check("done by a few words", ok and items[1]["done"] and items[1]["doneAt"] > 0, msg)
check("done twice refused", not server.tasks_op(items, "done", ref="tidy")[0])
check("undone reopens", server.tasks_op(items, "undone", ref=items[1]["id"])[0] and not items[1]["done"])
check("lev flips, same lev refused",
      server.tasks_op(items, "lev", ref="tidy", lev="high")[0] and items[1]["lev"] == "hi"
      and not server.tasks_op(items, "lev", ref="tidy", lev="high")[0])
check("unknown ref / op refused",
      not server.tasks_op(items, "done", ref="nope")[0] and not server.tasks_op(items, "explode")[0])
server.tasks_op(items, "done", ref="tidy")
check("clear drops done only", server.tasks_op(items, "clear")[0] and [i["text"] for i in items] == ["ship the chip"])
check("rm by id", server.tasks_op(items, "rm", ref=items[0]["id"])[0] and items == [])
full = []
for n in range(server.TASKS_MAX):
    server.tasks_op(full, "add", text=f"item {n}")
check("the cap refuses one more", len(full) == server.TASKS_MAX and not server.tasks_op(full, "add", text="extra")[0])

print("2. session apply + meta")
m = Mgr()
s = sess(m)
check("a fresh session has no list", s.tasks_meta() == {"hi": 0, "lo": 0, "done": 0, "at": 0.0, "items": []})
s.prompt_count = 5
s.tasks_apply("add", text="low one", lev="lo", via="agent")
s.tasks_apply("add", text="high one", via="agent")
s.tasks_apply("add", text="finished one", via="agent")
s.tasks_apply("done", ref="finished")
t = s.tasks_meta()
check("counts", (t["hi"], t["lo"], t["done"]) == (1, 1, 1) and t["at"] > 0, str(t))
check("order: open high, open low, done", [i["text"] for i in t["items"]] == ["high one", "low one", "finished one"])
check("each change saved + broadcast, and counts as a touch", m.saved == 4 and m.casts == 4 and s.tasks_turn == 5)
s.tasks_apply("done", ref="nope")
check("a refused op neither saves nor broadcasts", m.saved == 4 and m.casts == 4)

print("3. self_tasks")
server.MGR = m
code, out = server.self_tasks("c1", "list")
check("list: open items, high first", code == 200 and out.splitlines()[0].endswith("high  high one")
      and "finished" not in out, out)
code, out = server.self_tasks("c1", "list", want_all=True)
check("list --all shows done", code == 200 and "[x]" in out)
code, out = server.self_tasks("c1", "add", text="another", lev="lo")
check("add replies the line + what is left", code == 200 and "added [" in out and "(1 high, 2 low open)" in out, out)
check("refused → 409", server.self_tasks("c1", "done", ref="zzz")[0] == 409)
check("unknown session → 404", server.self_tasks("nope", "list")[0] == 404)

print("4. durable")
reg = json.loads(json.dumps(s.to_registry()))
s2 = server.ClaudeSession(Mgr(), cid="c1", session_id="x", resuming=False,
                          tasks=reg["tasks"], tasks_turn=reg["tasks_turn"])
check("registry round-trip", s2.to_registry()["tasks"] == reg["tasks"] and s2.tasks_turn == 5
      and s2.tasks_meta()["at"] > 0)
check("clone_for_respawn carries the list", s.clone_for_respawn().to_registry()["tasks"] == reg["tasks"])
check("junk in the registry is dropped", server.clean_tasks([{"id": "a", "text": ""}, "x", {"id": "b", "text": "ok", "lev": "??"}])
      == [{"id": "b", "text": "ok", "done": False, "lev": "hi", "created": 0.0, "doneAt": 0.0, "via": ""}])

print("5. tasks_note")
m = Mgr()
s = sess(m, "c5")
check("empty list says nothing", s.tasks_note("SessionStart", {"source": "compact"}) == ""
      and s.tasks_note("UserPromptSubmit", {}) == "")
s.tasks_apply("add", text="wire the nudge")
s.tasks_apply("add", text="polish", lev="lo")
check("fresh startup says nothing", s.tasks_note("SessionStart", {"source": "startup"}) == "")
n = s.tasks_note("SessionStart", {"source": "compact"})
check("after a compaction: the open items", "2 open item(s)" in n and "wire the nudge" in n and "polish" in n, n)
check("after a resume too", "wire the nudge" in s.tasks_note("SessionStart", {"source": "resume"}))
check("never at Stop", s.tasks_note("Stop", {}) == "")
s.tasks_turn = s.prompt_count = 0
quiet = []
for _ in range(server.TASKS_STALE_TURNS - 1):
    s.prompt_count += 1
    quiet.append(s.tasks_note("UserPromptSubmit", {}))
check("quiet while the list is fresh", quiet == [""] * (server.TASKS_STALE_TURNS - 1))
s.prompt_count += 1
n = s.tasks_note("UserPromptSubmit", {})
check("one reminder once it sat untouched", "hasn't changed" in n and "wire the nudge" in n, n)
s.prompt_count += 1
check("…then quiet again", s.tasks_note("UserPromptSubmit", {}) == "")
s.prompt_count += server.TASKS_STALE_TURNS
s.tasks_apply("done", ref="polish")
check("a touch resets the count", s.tasks_note("UserPromptSubmit", {}) == "")
s.tasks_apply("done", ref="wire")
s.prompt_count += 50
check("all done says nothing", s.tasks_note("UserPromptSubmit", {}) == ""
      and s.tasks_note("SessionStart", {"source": "resume"}) == "")
r = sess(m, "c6", check_of="c5")
r.tasks = server.clean_tasks([{"id": "a", "text": "x"}])
r.prompt_count = 99
check("a reviewer gets no notes", r.tasks_note("UserPromptSubmit", {}) == "")

print("6. the rule + the hook settings")
argv = server.ENGINES["claude"].argv(s)
check("a working session is born with the rule",
      "--append-system-prompt" in argv and server.TASKS_RULE in argv[argv.index("--append-system-prompt") + 1])
check("a reviewer is not", "--append-system-prompt" not in server.ENGINES["claude"].argv(r))
check("the rule names the command and both levels",
      "harness-tasks add" in server.TASKS_RULE and "--low" in server.TASKS_RULE and "harness-tasks done" in server.TASKS_RULE)
check("the wrap reconciles it", "harness-tasks" in server.WRAP_PROMPT)
import os
path = s._write_hook_settings()
hooks = json.loads(open(path).read())["hooks"]
os.unlink(path)
cmd = lambda ev: hooks[ev][0]["hooks"][0]["command"]
check("only SessionStart + UserPromptSubmit print /hook's reply (and never an error page)",
      all(">/dev/null 2>&1" not in cmd(e) and "curl -sSf" in cmd(e) for e in ("SessionStart", "UserPromptSubmit"))
      and all(">/dev/null 2>&1" in cmd(e) for e in ("Stop", "SessionEnd", "Notification", "PreToolUse", "PostToolUse")))

if FAILS:
    print(f"\n{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("\nall good")
