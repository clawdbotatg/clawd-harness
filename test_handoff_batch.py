#!/usr/bin/env python3
"""The per-sweep handoff budget: a drained plan evacuates as a queue, not a herd.

Why this exists: on 2026-08-17 three subscriptions were spent in a day and the
audit VMs were blamed. They were idle. The cost was the router — a handoff
respawns the session with --resume, so each one re-ingests that session's whole
context, and the sweep moved EVERY session on a drained plan in one pass. Ten
simultaneous re-ingests landed on the fresh pool, spent enough of it to drain
that pool too, and the next sweep marched everyone back: the day's log shows
sub4->clawd 89 times and clawd->sub4 67 times. Only the capability-evacuation
path had a batch cap; the drained rescue, the hot evacuation and the rebalance
had none.

These tests pin the three properties that make the cap safe rather than merely
smaller:

  * a 10-session evacuation moves SUB_HANDOFF_BATCH per sweep, not all ten,
  * successive sweeps still drain the backlog (the cap delays, never strands),
  * a rescue outranks an optional rebalance when the budget is short.

Like test_fable_gate.py this constructs NO SessionManager — it binds the real
_handoff_sweep to a stub (a real manager would --resume this machine's live
sessions). Nothing spawns, no registry is touched.

    python3 test_handoff_batch.py
"""
import sys
import types

import server


class FakeEng:
    routes_accounts = True
    def bg_probe(self, s):  # noqa: D102
        return False


class FakeSession:
    def __init__(self, cid, account):
        self.cid = cid
        self.account = account
        self.alive = True
        self.busy = False
        self.bg = False
        self.ceremony = False
        self.last_handoff = 0.0
        self.last_active = 0.0
        self.eng = FakeEng()


class FakeAccount:
    def __init__(self, name, pct, broken=False, fable=True):
        self.name = name
        self.usage = {"pct": pct}
        self.broken = broken
        self._fable = fable
    def routable(self):
        return self._fable


class FakeMgr:
    """Just enough surface for the real _handoff_sweep to run."""
    def __init__(self, accounts, sessions, best):
        import threading
        self.lock = threading.RLock()
        self.accounts = {a.name: a for a in accounts}
        self.sessions = {s.cid: s for s in sessions}
        self._best = best
        self.moves = []                       # (cid, dest) in order
    def _best_account(self):
        return self._best
    def _handoff(self, s, best, why=None):
        self.moves.append((s.cid, best.name))
        s.account = best.name                 # what a real handoff ends up doing
        s.last_handoff = 0.0                  # cooldown anchored at epoch = eligible
    def _rebalance_win(self, name, best):
        return None                           # no optional moves unless a test says so

    sweep = server.SessionManager._handoff_sweep


def build(n_drained=10, batch=None):
    drained = FakeAccount("slop", pct=100.0)
    fresh   = FakeAccount("ef",   pct=1.0)
    sess = [FakeSession(f"cid{i:02d}", "slop") for i in range(n_drained)]
    return FakeMgr([drained, fresh], sess, best="ef")


FAILED = []
def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(label)


print(__doc__.strip().splitlines()[0])
print()

# ── 1. the herd becomes a queue ────────────────────────────────────────
print("a 10-session evacuation is capped per sweep")
server.SUB_HANDOFF_BATCH = 2
m = build(10)
m.sweep()
check("one sweep moves exactly the batch cap (2), not all 10",
      len(m.moves) == 2, f"moved {len(m.moves)}")

# ── 2. the cap delays, it never strands ────────────────────────────────
print("\nsuccessive sweeps drain the backlog")
for _ in range(9):
    m.sweep()
check("10 sessions all reach the fresh pool after enough sweeps",
      len(m.moves) == 10, f"moved {len(m.moves)}")
check("every session ended on the fresh pool",
      all(s.account == "ef" for s in m.sessions.values()))
check("no session moved twice",
      len({c for c, _ in m.moves}) == 10, f"{len(m.moves)} moves, "
      f"{len({c for c, _ in m.moves})} distinct")

# ── 3. unlimited is still reachable (old behaviour, opt-in) ────────────
print("\nSUB_HANDOFF_BATCH=0 restores the unbatched sweep")
server.SUB_HANDOFF_BATCH = 0
m = build(10)
m.sweep()
check("0 = no cap, all 10 move in one sweep", len(m.moves) == 10,
      f"moved {len(m.moves)}")

# ── 4. a rescue outranks an optional rebalance ─────────────────────────
print("\nrescues win the budget over optional rebalances")
server.SUB_HANDOFF_BATCH = 2
drained = FakeAccount("slop", pct=100.0)
healthy = FakeAccount("sub4", pct=10.0)
fresh   = FakeAccount("ef",   pct=1.0)
# two stuck on the drained plan, plus three merely-rebalanceable ones listed FIRST
sess = ([FakeSession(f"reb{i}", "sub4") for i in range(3)] +
        [FakeSession(f"resc{i}", "slop") for i in range(2)])
m = FakeMgr([drained, healthy, fresh], sess, best="ef")
m._rebalance_win = lambda name, best: "reset sooner"   # every healthy one wants to move
m.sweep()
moved = {c for c, _ in m.moves}
check("both drained-plan sessions moved first",
      moved == {"resc0", "resc1"}, f"moved {sorted(moved)}")

print()
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("all handoff-batch tests passed")
