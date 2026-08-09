#!/usr/bin/env python3
"""The model-capability gate: a subscription that can't do fable is skipped.

Why this exists: on 2026-08-09 the slop@buidlguidl.com org changed plans and
stopped carrying Fable. Nothing in the router noticed — every routing decision
in server.py reads percentages, and a fable-less pool sitting at 3% used looks
like the BEST pool there is. Sixteen live sessions had been routed onto it.

So the gate is capacity-blind by design, and these tests pin the three things
that make it safe rather than merely correct:

  * it convicts only on a POSITIVE reading (unknown never blocks),
  * it never strands the router (an all-incapable roster still routes),
  * it beats headroom (a capable pool at 95% wins over an incapable one at 0%).

Runs entirely on the pure ranking helpers — it constructs no SessionManager,
touches no registry, and spawns nothing (see the scratch-registry trap: a real
manager here would --resume the machine's live sessions).

    python3 test_fable_gate.py
"""
import sys
import time
import types

import server


def acct(name, pct, fable=True, reset_in_h=48, seen=0.0, **kw):
    """A ready Account with a synthetic usage snapshot. `fable=None` = a pool
    we've never had a good reading for (no windows at all). `seen` = epoch of
    a previously observed fable window (0 = never)."""
    a = server.Account(name, config_dir=f"/tmp/{name}", ready=True,
                       fable_seen=seen, **kw)
    if fable is None:
        a.usage = {"pct": pct, "checkedAt": time.time()}
        return a
    resets = server.datetime.datetime.fromtimestamp(
        time.time() + reset_in_h * 3600,
        server.datetime.timezone.utc).isoformat()
    windows = [{"key": "seven_day", "label": "7d", "used": pct, "resets": resets}]
    if fable:
        windows.append({"key": "weekly_scoped_fable", "label": "7d fable",
                        "used": pct, "resets": resets})
    a.usage = {"pct": pct, "windows": windows,
               "checkedAt": time.time(), "goodAt": time.time()}
    return a


def mgr(*accounts):
    """A duck-typed stand-in carrying only what the ranking helpers touch."""
    m = types.SimpleNamespace(
        accounts={a.name: a for a in accounts},
        lock=server.threading.RLock(),
        _stranded_warned=False)
    for meth in ("_route_key", "_routable_first", "_best_account"):
        setattr(m, meth, getattr(server.SessionManager, meth).__get__(m))
    m.KEY_CAP = server.SessionManager.KEY_CAP
    return m


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# ── detection ────────────────────────────────────────────────────────────────
@case
def test_detects_fable_from_the_scoped_window():
    """The live signal: a plan that carries fable advertises a weekly fable
    window even at 0% used (verified against sub3), so ABSENCE is entitlement
    rather than merely non-use."""
    assert server._fable_state(acct("y", 0.0, fable=True).usage) is True
    assert server._fable_state(acct("n", 3.0, fable=False).usage) is False


@case
def test_no_reading_is_unknown_not_incapable():
    """The whole-fleet failure mode: if the endpoint ever stops emitting scoped
    limits, an 'absent means no' rule would convict every pool at once. A
    windowless snapshot must read as unknown, and unknown must stay routable."""
    assert server._fable_state(None) is None
    assert server._fable_state({"pct": 5.0}) is None
    assert acct("blind", 5.0, fable=None).routable() is True


@case
def test_the_label_is_not_the_subscription():
    """The mistake that produced (and then nearly un-produced) this gate.

    `sub4` on one box and `sub4` on another are CONFIG-DIR LABELS, and they
    routinely hold logins into different orgs. Reading a "contradiction"
    between two boxes' same-named accounts is how a correct verdict got
    diagnosed as a false positive on 2026-08-09 — the fix that followed
    weakened the gate on a premise that was never true. Pools are compared by
    ORG uuid, never by name."""
    a = acct("sub4", 91.0, fable=False, org="18f36efd")     # the fable-less org
    b = acct("sub4", 6.0, fable=True, org="94f7f5f0")       # a different plan entirely
    assert a.org != b.org, "same label, different subscription — the whole trap"
    assert a.fable() is False and b.fable() is True
    assert a.routable() is False and b.routable() is True


@case
def test_a_hot_reading_still_convicts():
    """There is NO evidence that being at a limit suppresses the scoped
    windows (see the note above _fable_state). Gating conviction on a
    'healthy' reading re-opened the original bug: a fable-less pool at 91% is
    UNDER the 97% hot bar, so nothing else would have skipped it."""
    hot = acct("slop-hot", 91.0, fable=False)
    assert hot.fable() is False and hot.routable() is False


@case
def test_a_healthy_reading_still_convicts():
    """The correction must not neuter the gate: clawdteam sat at 47% with no
    fable window and genuinely could not run it (verified with a real
    `claude -p --model fable` call)."""
    healthy = acct("clawdteam", 47.0, fable=False)
    assert healthy.fable() is False and healthy.routable() is False


@case
def test_a_recent_sighting_outweighs_one_bad_reading():
    """Plan entitlement doesn't flicker between polls. Having SEEN fable
    minutes ago, a payload that stops mentioning it is a degraded payload,
    not a downgraded plan."""
    a = acct("blip", 5.0, fable=False, seen=time.time() - 60)
    assert a.fable() is True and a.routable() is True


@case
def test_stickiness_expires_so_a_real_downgrade_is_caught():
    """The slop org really did change plans. A sighting from last week must
    not protect it forever."""
    stale = acct("slop", 5.0, fable=False,
                 seen=time.time() - server.FABLE_STICKY - 1)
    assert stale.fable() is False, "an expired sighting must stop shielding"


@case
def test_recording_a_reading_stamps_the_sighting():
    """The stickiness is only possible if the stamp is actually written on the
    reading that saw fable — record_usage is the single choke point for that."""
    a = server.Account("x", ready=True)
    a.record_usage(3.0, [{"key": "seven_day", "label": "7d", "used": 3.0}])
    assert a.fable_seen == 0.0, "no fable window → no stamp"
    a.record_usage(3.0, [{"key": "weekly_scoped_fable", "label": "7d fable",
                          "used": 0.0}])
    assert a.fable_seen > 0, "a fable window must stamp the sighting"
    assert "fable_seen" in a.to_registry(), "and it must survive a restart"


@case
def test_manual_overrides_beat_the_heuristic():
    """It's a heuristic on an undocumented endpoint — both escape hatches have
    to actually override it."""
    a, b = acct("liar", 3.0, fable=True), acct("wronged", 3.0, fable=False)
    server.SUB_NO_FABLE.add("liar")
    server.SUB_FABLE_OK.add("wronged")
    try:
        assert a.fable() is False and a.routable() is False
        assert b.fable() is True and b.routable() is True
    finally:
        server.SUB_NO_FABLE.discard("liar")
        server.SUB_FABLE_OK.discard("wronged")


# ── ranking ──────────────────────────────────────────────────────────────────
@case
def test_capability_outranks_headroom():
    """The exact shape of the 08-09 incident: the fable-less pool is also the
    emptiest one, so every percentage-based rule picks it."""
    m = mgr(acct("slop", 3.0, fable=False), acct("good", 95.0, fable=True))
    assert m._best_account() == "good"


@case
def test_capable_pools_still_rank_by_the_normal_rules():
    """The gate must not disturb the ordering it isn't about: among capable
    pools, the soonest weekly reset still wins (use-it-or-lose-it)."""
    m = mgr(acct("later", 5.0, reset_in_h=100), acct("sooner", 40.0, reset_in_h=10))
    assert m._best_account() == "sooner"


@case
def test_gate_never_strands_the_router():
    """Routing to a fable-less plan is bad; routing nowhere is worse. With no
    capable pool anywhere, the roster still resolves (on capacity alone)."""
    m = mgr(acct("a", 70.0, fable=False, reset_in_h=90),
            acct("b", 4.0, fable=False, reset_in_h=10))
    assert all(x.fable() is False for x in m.accounts.values()), "setup"
    assert m._best_account() == "b", "all-incapable must still rank normally"
    assert m._stranded_warned is True, "the fallback must announce itself"


@case
def test_unknown_pool_beats_a_convicted_one():
    """Unknown is not a conviction: a blind pool must still outrank one we
    positively know can't do the work."""
    m = mgr(acct("known-bad", 1.0, fable=False), acct("blind", 60.0, fable=None))
    assert m._best_account() == "blind"


@case
def test_gate_off_restores_pure_capacity_routing():
    """SUB_REQUIRE_FABLE=0 has to be a real off switch, not a preference."""
    m = mgr(acct("slop", 3.0, fable=False), acct("good", 95.0, fable=True))
    server.SUB_REQUIRE_FABLE = False
    try:
        assert m._best_account() == "slop"
    finally:
        server.SUB_REQUIRE_FABLE = True


@case
def test_meta_reports_the_verdict_to_the_ui():
    """The panel keeps showing the account (the user asked for it to stay in
    the list) — so the card needs the reason it's greyed out."""
    m = acct("slop", 3.0, fable=False).meta()
    assert m["fable"] is False and m["routable"] is False
    assert m["status"] == "ready", "skipped by the router ≠ signed out"
    assert acct("good", 3.0).meta()["routable"] is True


@case
def test_route_key_positional_names_match_the_tuple():
    """_maybe_autoswitch indexes _route_key's tuple by these names; a term
    added without moving them would silently compare the wrong fields."""
    S, a = server.SessionManager, acct("x", 98.0, fable=False, reset_in_h=1)
    k = mgr(a)._route_key(a)
    assert len(k) == 5
    assert k[S.KEY_CAP] is True and k[S.KEY_HOT] is True
    assert k[S.KEY_NORESET] is False and k[S.KEY_PCT] == 98.0
    assert k[S.KEY_RESET] > time.time()


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)
