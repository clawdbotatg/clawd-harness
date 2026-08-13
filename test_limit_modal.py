#!/usr/bin/env python3
"""The extra-usage-credits wall: the limit scan must catch the 2026-08 modal.

Why this exists: when a model-scoped weekly window runs dry (the Fable weekly
was the first), the CLI no longer paints the classic one-line limit banner —
it paints a blocking ink dialog ("You've reached your <model> limit … uses
usage credits") with numbered options and an Enter-confirms footer, and waits.
The original _scan_for_limit collapsed whitespace and matched spaced needles,
which is right for a styled one-line banner but matches NOTHING here: ink pads
dialogs with cursor motion, not spaces, so de-ANSI'd dialog text arrives with
the words run together (the resume-gate lesson, measured there against a real
capture). Result: sessions sat on the modal forever, prompts got eaten, and
no rescue ever fired — the 2026-08-13 "hitting the Fable limit everywhere"
incident.

The fix scans a rolling RAW byte window re-stripped each read (chunk splits
inside escape sequences leak junk into per-chunk text) and matches two forms:
_LIMIT_BANNER_RE against space-collapsed text (the classic banner, plus a
spaced "reached your … limit" alternate) and _LIMIT_MODAL_RE against
whitespace-stripped text (the credits dialog). Unlike the resume gate, this
scan HAS an oracle: every match is handed to rescue_limit_wall, which confirms
against the live usage endpoint before moving anything — so text merely
QUOTING the modal (this file does) is a no-op on a cool pool, and the needle
does not need to carry the guard alone.

Runs on a stand-in session object — no SessionManager, no registry, nothing
spawned.

    python3 test_limit_modal.py
"""
import inspect
import re
import time
import types

import server

FAILED = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def ink_paint(lines):
    """Render text the way ink lays out a dialog: every space becomes a
    cursor-forward escape, lines are cursor-positioned, styling interleaves —
    so the de-ANSI'd result is space-free run-together words."""
    out = b""
    for i, line in enumerate(lines):
        out += b"\x1b[%d;3H\x1b[38;5;246m" % (i + 4)
        first = True
        for word in line.split(" "):
            if not first:
                out += b"\x1b[1C"
            out += word.encode()
            first = False
        out += b"\x1b[0m"
    return out


# The dialog as painted 2026-08-13 (wording from a live hit; options omitted —
# the needle deliberately keys on the headline + credits sentence, and the
# rescue re-confirms against the endpoint regardless).
MODAL = ink_paint([
    "You've reached your Fable 5 limit",
    "",
    "You've used your included Fable 5 usage for this week. Continuing on",
    "Fable 5 uses usage credits — you have $19.79 in credits.",
    "",
    "Learn more: https://support.claude.com/en/articles/12429409",
])

OLD_BANNER = b"\x1b[1mYou've hit your session limit \xc2\xb7 resets 3pm\x1b[0m"


def fake_session(ceremony=False):
    """A stand-in with only what _scan_for_limit touches. `rescued` collects
    rescue_limit_wall invocations (fired on a thread, so poll to read it)."""
    rescued = []
    s = types.SimpleNamespace(
        cid="deadbeef-cafe", account="sub3", ceremony=ceremony,
        _limit_raw=b"", _limit_seen_at=0.0, rescued=rescued,
        manager=types.SimpleNamespace(rescue_limit_wall=rescued.append),
    )
    return s


def feed(s, data, chunk=None):
    scan = server.ClaudeSession._scan_for_limit
    if chunk is None:
        scan(s, data)
        return
    for i in range(0, len(data), chunk):
        scan(s, data[i:i + chunk])


def wait_rescued(s, n=1, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(s.rescued) >= n:
            return True
        time.sleep(0.02)
    return len(s.rescued) >= n


def main():
    print("needle vs ink rendering:")
    flat = re.sub(r"\s+", "", server._PTY_ANSI_RE.sub(b"", MODAL).decode("utf-8", "ignore"))
    check("ink paint really is space-free after de-ANSI", " " not in flat)
    check("the spaced banner needle does NOT see the dialog (why the modal needle exists)",
          not server._LIMIT_BANNER_RE.search(
              re.sub(r"\s+", " ", server._PTY_ANSI_RE.sub(b"", MODAL).decode("utf-8", "ignore"))))
    check("_LIMIT_MODAL_RE matches the flattened dialog", bool(server._LIMIT_MODAL_RE.search(flat)))
    check("curly-apostrophe variant matches too",
          bool(server._LIMIT_MODAL_RE.search(flat.replace("You've", "You’ve"))))

    print("scan behavior:")
    s = fake_session()
    feed(s, MODAL, chunk=3)   # 3-byte chunks: every escape sequence gets split
    check("modal fed in 3-byte chunks fires the rescue", wait_rescued(s))
    check("exactly once", len(s.rescued) == 1, f"got {len(s.rescued)}")
    check("rescue is handed the session itself", s.rescued[0] is s)

    s1 = fake_session()
    feed(s1, MODAL)           # whole dialog in one read
    check("match clears the raw window", wait_rescued(s1) and s1._limit_raw == b"")

    feed(s, MODAL)            # a repaint inside the cooldown
    time.sleep(0.3)
    check("a repaint inside BOUNCE_COOLDOWN is suppressed", len(s.rescued) == 1)

    s2 = fake_session()
    feed(s2, OLD_BANNER, chunk=5)
    check("the classic one-line banner still fires", wait_rescued(s2))

    s3 = fake_session()
    feed(s3, b"just some normal output about limits and usage and credits", chunk=7)
    time.sleep(0.2)
    check("innocuous text mentioning limits/credits fires nothing", not s3.rescued)

    s4 = fake_session(ceremony=True)
    feed(s4, MODAL)
    time.sleep(0.2)
    check("a sign-in ceremony session is never rescued", not s4.rescued)

    print("guard rails (source checks):")
    pump = inspect.getsource(server.ClaudeSession._pump_pty)
    check("the scan stays fenced behind routes_accounts",
          "routes_accounts" in pump.split("_scan_for_limit")[0].rsplit("if", 1)[-1]
          or "routes_accounts:" in pump)
    rescue = inspect.getsource(server.SessionManager.rescue_limit_wall)
    check("rescue_limit_wall still confirms against the usage endpoint",
          "_fetch_usage" in rescue)

    print()
    if FAILED:
        print(f"FAILED: {len(FAILED)} — {FAILED}")
        return 1
    print("all good")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
