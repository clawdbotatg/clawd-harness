#!/usr/bin/env python3
"""A model-scoped limit reply ("You've reached your Fable limit") must move
the session and retype the prompt — and a resume repaint must never trip it.

2026-09-23 incident (head, 4:51 PM and 5:00 PM): two sessions on sub4 (7d fable
100%) answered prompts with "You've reached your Fable limit. Run
/usage-credits to continue or switch models with /model." and sat there while
austinmax (EF org, 36%, fable 29%) was signed in on the same box. The reply
is a <synthetic> assistant line with ORDINARY hooks: the send watchdog saw a
healthy turn, the PTY scan missed it (sub4 was never quarantined), and even a
caught wall would have dropped the prompt (the hook-count bounce test says a
turn with hooks "ran"). And sub4 was not the only pool out: something had put
the EF pool out of routing — the PTY scan re-reading a --resume repaint of an
old limit line, confirmed by a 429 (believed by design), is a path that does
exactly that, so the scan now arms only once a prompt goes in.

No SessionManager is constructed (it would resume live sessions).
"""
import datetime
import json
import threading
import time
import types

import server

FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(label)


NOW = time.time()
TEXT = ("You've reached your Fable limit. Run /usage-credits to continue or "
        "switch models with /model.")


def iso(seconds):
    return datetime.datetime.fromtimestamp(
        NOW + seconds, datetime.timezone.utc).isoformat()


def acct(name, org, five, week, fable, week_reset):
    a = server.Account(name, config_dir=f"/tmp/{name}", ready=True, org=org)
    a.record_usage(max(five, week, fable), [
        {"key": "five_hour", "label": "5h", "used": five, "resets": iso(3 * 3600)},
        {"key": "seven_day", "label": "7d", "used": week, "resets": iso(week_reset)},
        {"key": "weekly_scoped_fable", "label": "7d fable", "used": fable,
         "resets": iso(week_reset)},
    ], NOW)
    return a


def synth(text, ts):
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {
        "model": "<synthetic>", "role": "assistant",
        "content": [{"type": "text", "text": text}]}})


print("the reply is recognized:")
check("the incident text matches", server._LIMIT_SYNTH_RE.search(TEXT))
check("the classic session wall matches too",
      server._LIMIT_SYNTH_RE.search("You've hit your session limit · resets 2pm"))
check("ordinary prose doesn't", not server._LIMIT_SYNTH_RE.search(
    "we reached the limit of what the Fable model can do"))

print("\nthe rescue (incident numbers):")
sub4 = acct("sub4", "CLAWD", 0, 66, 100, 86400)
am = acct("austinmax", "EF", 4, 36, 29, 6 * 86400)
m = types.SimpleNamespace(accounts={a.name: a for a in (sub4, am)}, sessions={},
                          lock=threading.RLock(), _stale_route_noted="", moved=[])
m.save_registry = lambda: None
m.broadcast_accounts = lambda: None
for name in ("_route_key", "_routable_first", "_candidates", "_pick_pool",
             "_best_account", "_mark_pool_walled", "_redeliver",
             "rescue_limit_wall"):
    setattr(m, name, getattr(server.SessionManager, name).__get__(m))
m._stay_put_log = lambda *a: m.moved.append(("STAY",))
m._blind_alternative = lambda **kw: None
check("before: the router already prefers austinmax (sub4 is hot)",
      m._best_account() == "austinmax")


class Ready:
    def wait(self, timeout):
        return True


sent = []
PROMPT = "so are you saying we are safe to make a tweet"
# the turn RAN: hooks advanced past the prompt, busy cleared by the Stop
s = types.SimpleNamespace(
    cid="pashov", account="sub4", config_dir="/tmp/sub4", alive=True,
    ceremony=False, eng=types.SimpleNamespace(routes_accounts=True),
    last_bounce_rescue=0.0, _limit_kind="weekly", busy=False,
    hook_count=7, hooks_at_prompt=5, last_prompt=PROMPT)
m.sessions[s.cid] = s


def handoff(old, target, why="", fresh_if_empty=False):
    m.sessions[old.cid] = types.SimpleNamespace(
        cid=old.cid, account=target.name, alive=True, _started_evt=Ready(),
        send_message=sent.append)
    m.moved.append(target.name)


m._handoff = handoff
old_fetch, old_sleep = server._fetch_usage, server.time.sleep
try:
    server._fetch_usage = lambda *a, **kw: (100.0, sub4.usage["windows"])
    server.time.sleep = lambda _s: None
    m.rescue_limit_wall(s, PROMPT)
finally:
    server._fetch_usage, server.time.sleep = old_fetch, old_sleep
check("moves to austinmax", m.moved == ["austinmax"], f"moved={m.moved}")
check("retypes the eaten prompt exactly once, though its hooks fired",
      sent == [PROMPT], f"sent={sent}")
fable_reset = server._parse_reset(sub4.usage["windows"][2]["resets"])
check("sub4 is quarantined to the FABLE window's reset",
      abs(sub4.walled_until - (fable_reset + server.WALL_RESET_GRACE)) < 1)

print("\nthe tripwires:")
fired = []
sess = server.ClaudeSession.__new__(server.ClaudeSession)
sess.cid, sess.account, sess.ceremony = "x" * 8, "sub4", False
sess.engine = "claude"
sess.manager = types.SimpleNamespace(rescue_limit_wall=lambda *a: fired.append(a))
sess._limit_raw, sess._limit_seen_at, sess._limit_armed = b"", 0.0, False
sess._launched_at = NOW
sess.last_prompt = PROMPT
sess._scan_for_limit(TEXT.encode())
time.sleep(0.05)
check("a resume repaint of an old limit line does NOT trip the PTY scan",
      fired == [], f"fired={fired}")
sess._limit_armed = True
sess._scan_for_limit(TEXT.encode())
time.sleep(0.05)
check("once a prompt went in, the same paint does", len(fired) == 1)

old_line = synth(TEXT, iso(-3600))
new_line = synth(TEXT, iso(1))
check("a transcript line from before this launch is history",
      not sess._live_line(None, old_line))
check("a line from this launch is live", sess._live_line(None, new_line))
fired.clear()
sess._limit_seen_at = 0.0
sess._on_limit_transcript(new_line)
time.sleep(0.05)
check("the transcript trip hands the prompt to the rescue",
      fired and fired[0][1] == PROMPT, f"fired={fired}")
src = open(server.__file__).read()
check("write() arms the scan on Enter",
      'if not self._limit_armed and b"\\r" in data:' in src)

print()
if FAILED:
    print(f"FAIL: {len(FAILED)}")
    raise SystemExit(1)
print("ok")
