#!/usr/bin/env python3
"""test_emoji_reroll.py — two boxes must not wear the same project badge.

Each box badges only against its OWN projects, so bambu-lab (clawd-bambu) and
clawd-pico-case (clawd-omen) both drew 🖨️ (09-29), and identical codes can't
be split by the page's trimming. The page names the loser and sends it
`emojiReroll` with every code worn elsewhere. Guards, on a sandboxed copy of
server.py and fakes — never the live daemon:
  1. 🖨 and 🖨️ (VS16) compare equal, so a model answer differing only in VS16
     counts as a collision;
  2. emoji_reroll flags the project (ignored when its code isn't in the list,
     and throttled), and the next sweep re-rolls it FIRST, against the other
     box's codes including its own current one;
  3. a normal refresh of another project keeps its own code eligible even when
     it appears in the stored fleet list.
Exits non-zero on any failure.
"""
import sys, types, shutil, tempfile, threading   # (one line: gitleaks bip39 rule)
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent
FAILS = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


tmp = Path(tempfile.mkdtemp(prefix="emoji-reroll-test-"))
shutil.copy(REPO / "server.py", tmp / "server.py")
spec = importlib.util.spec_from_file_location("server_sandbox_emoji", tmp / "server.py")
srv = importlib.util.module_from_spec(spec)
sys.modules["server_sandbox_emoji"] = srv
spec.loader.exec_module(srv)

# 1. normalization in generate_project_emoji
answers = iter(["🖨", "🧵🖨️"])
srv._llm_json = lambda sys_p, text: {"emoji": next(answers)}
check("VS16-less answer counts as a collision → stricter retry",
      srv._emoji_norm(srv.generate_project_emoji("ctx", ["🖨️"])) == "🧵🖨")

# 2. reroll flag + sweep order
srv.BANKR_API_KEY, srv.BANKR_BASE_URL = "k", "u"
a = srv.Project("pa", "bambu-lab", str(tmp), emoji="🖨️", emoji_at=9e18)
b = srv.Project("pb", "other", str(tmp), emoji="🔬", emoji_at=9e18)
sm = srv.SessionManager.__new__(srv.SessionManager)
sm.lock = threading.RLock()
sm.projects = {"pa": a, "pb": b}
sm.sessions = {}
spawned = []
sm._ordered_projects = lambda: [b, a]
sm._ordered = lambda: []
srv.threading = types.SimpleNamespace(Thread=lambda target, args, daemon: types.SimpleNamespace(
    start=lambda: spawned.append(args)))

sm.emoji_reroll("pa", ["🍄"])
check("code not in the list → no re-roll", a.emoji_reroll is False)
sm.emoji_reroll("pa", ["🖨", "🍄"])
check("code worn elsewhere → flagged", a.emoji_reroll is True)
sm.emoji_sweep()
check("sweep picks the flagged project", spawned and spawned[-1][0] is a, str(spawned))
taken = spawned[-1][1] if spawned else []
check("…avoiding the other box's codes, its own included",
      "🖨" in taken and "🍄" in taken and "🔬" in taken, str(taken))
a.emoji_reroll = False
sm.emoji_reroll("pa", ["🖨"])
check("second ask inside EMOJI_REROLL_S is throttled", a.emoji_reroll is False)

# 3. a normal refresh keeps its own code eligible
b.emoji_at = 0.0
sm._emoji_busy = False
sm._emoji_scan_at = 0.0
sm._fleet_emoji_taken = ["🔬", "🍄"]
sm.emoji_sweep()
check("refresh picks the stale project", spawned[-1][0] is b)
check("…and doesn't forbid its own code", "🔬" not in spawned[-1][1] and "🍄" in spawned[-1][1],
      str(spawned[-1][1]))

sys.exit(1 if FAILS else 0)
