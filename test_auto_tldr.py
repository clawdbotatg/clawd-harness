#!/usr/bin/env python3
"""The auto-TLDR trigger: 'I came back to a wall of text and wish someone had
tapped the tldr chip while I was gone' (2026-08-16). server.wants_auto_tldr is
the pure decision — these tests pin its thresholds so a tweak can't quietly
turn every two-line 'Done. Anything else?' into an extra billed turn, or stop
firing on the single-monster-paragraph reply that motivated the feature.

Runs on the pure helper only — constructs no SessionManager, spawns nothing
(the scratch-registry trap: a real manager here would --resume live sessions).

    python3 test_auto_tldr.py
"""
import server

FAILS = []


def check(name, cond):
    print(("  ok  " if cond else "  FAIL") + " " + name)
    if not cond:
        FAILS.append(name)


para = "This paragraph talks about the thing in enough words to be real. " * 3

print("wants_auto_tldr:")
check("empty reply never fires", not server.wants_auto_tldr(""))
check("None never fires", not server.wants_auto_tldr(None))
check("short single paragraph doesn't fire",
      not server.wants_auto_tldr("Done. The fix is pushed."))
check("two SHORT paragraphs don't fire (under AUTO_TLDR_MIN)",
      not server.wants_auto_tldr("Done.\n\nAnything else?"))
check("two real paragraphs fire",
      server.wants_auto_tldr(para + "\n\n" + para))
check("blank lines with spaces still split paragraphs",
      server.wants_auto_tldr(para + "\n   \n" + para))
check("one monster paragraph fires (AUTO_TLDR_LONG)",
      server.wants_auto_tldr("x" * (server.AUTO_TLDR_LONG + 1)))
check("one medium paragraph (over MIN, under LONG) doesn't fire",
      not server.wants_auto_tldr("y" * (server.AUTO_TLDR_LONG - 1)))
check("many single-\\n lines are ONE paragraph (a list isn't a wall)",
      not server.wants_auto_tldr(
          "\n".join(["- item %d, a bounded line" % i for i in range(10)])[
              :server.AUTO_TLDR_LONG - 1]))

print("judge:")
_real_llm = server._llm_json
_calls = []
def _no(*a, **k):
    _calls.append(1)
    return {"tldr": False}
server._llm_json = _no
check("a long reply never reaches the judge — a wall always gets the TLDR",
      server.judge_auto_tldr("z" * (server.AUTO_TLDR_JUDGE_MAX + 1)) is True
      and not _calls)
check("a short reply the judge calls tight is skipped",
      server.judge_auto_tldr("z" * server.AUTO_TLDR_JUDGE_MAX) is False)
server._llm_json = lambda *a, **k: None
check("judge unreachable -> the TLDR fires",
      server.judge_auto_tldr("z" * 400) is True)
server._llm_json = _real_llm
check("the judge's reach stays near one phone screen",
      server.AUTO_TLDR_MIN < server.AUTO_TLDR_JUDGE_MAX <= 1000)

print("knobs:")
check("feature defaults ON (the user asked to try it)", server.AUTO_TLDR)
check("the tap is tldr-shaped (the arming guard keys on this prefix)",
      server.AUTO_TLDR_TEXT.lower().startswith("tldr"))
check("the tap demands the no-slop style",
      "NO SLOP" in server.AUTO_TLDR_TEXT)
check("MIN below LONG (the two-paragraph path must be reachable)",
      server.AUTO_TLDR_MIN < server.AUTO_TLDR_LONG)

if FAILS:
    print("\n%d FAILED" % len(FAILS))
    raise SystemExit(1)
print("\nall passed")
