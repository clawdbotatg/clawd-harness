#!/usr/bin/env python3
"""The resume gate: a backgrounded session answers claude's compact modal itself.

Why this exists: CLI 2.1.226 resumes a long session (older than 70 min AND over
100k estimated tokens) onto a modal titled "This session is 1d 17h old and 438k
tokens.", offering three numbered options (summarize / resume as-is / stop
asking) with the first preselected, footed by an Enter-to-confirm hint — and
then waits. (The option list is quoted verbatim nowhere in this repo, and one
of the tests below is what keeps it that way; see the false-positive note.)

In a browser harness nobody is there to press Enter: every resume
path (daemon restart, graceful self-restart, account handoff, every rescue
respawn) hits it on exactly the long-lived sessions that matter, and the
session sits resumed-but-frozen until a human opens the tab. Worse, a prompt
delivered into that modal is not inert — the options are NUMBERED, so text
beginning "3" could pick "Don't ask me again" and its CR would confirm it.

Option 1 runs plain /compact, so answering it is both the unblocking move and
the cheap one — which is the whole feature: come back to a session that already
compacted itself instead of one waiting to be told to.

These tests pin the parts that are easy to re-break, against a REAL capture of
the modal's bytes (test_resume_gate_capture.bin — 885 bytes of PTY output from
`claude --resume` on a 628k-token session, not a hand-typed approximation,
because the needle's whole difficulty is how ink renders):

  * the needle matches those real bytes, INCLUDING when they arrive split
    across chunk boundaries (the modal never lands in one read),
  * it survives the rendering trap: ink pads with cursor motion, not spaces,
    so de-ANSI'd text is space-free and a spaced needle matches nothing,
  * prose that QUOTES the dialog (this file; the CLAUDE.md section) does not
    trip it — there is no confirming oracle for this modal, so the needle
    carries the guard alone,
  * it is resume-only, one-shot, and disarmed by any write to the PTY (so our
    CR can never split a harness send's text from its own submitting CR),
  * a non-claude engine opts out entirely.

Runs on a stand-in session object — it constructs no SessionManager, touches no
registry and spawns nothing (see the scratch-registry trap: a real manager here
would --resume the machine's live sessions).

    python3 test_resume_gate.py
"""
import os
import sys
import time
import types

import server

HERE = os.path.dirname(os.path.abspath(__file__))
CAPTURE = os.path.join(HERE, "test_resume_gate_capture.bin")

FAILED = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def fake_session(engine="claude", armed=True, window=None):
    """A stand-in with only what _scan_for_resume_gate touches. `sent` collects
    keystrokes so a test can assert exactly one bare CR left the harness."""
    s = types.SimpleNamespace(
        cid="deadbeef-cafe", account="sub3", sent=[],
        _gate_raw=b"",
        _gate_deadline=(time.time() + (window or server.RESUME_GATE_WINDOW)) if armed else 0.0,
        eng=types.SimpleNamespace(
            resume_gate_key=server.ClaudeEngine.resume_gate_key if engine == "claude"
            else server.Engine.resume_gate_key),
    )
    s.write = s.sent.append
    return s


def feed(s, data, chunk=None):
    """Push bytes through the real scan, optionally in fixed-size chunks."""
    scan = server.ClaudeSession._scan_for_resume_gate
    if chunk is None:
        scan(s, data)
        return
    for i in range(0, len(data), chunk):
        scan(s, data[i:i + chunk])


def main():
    raw = open(CAPTURE, "rb").read()
    print(f"resume gate — real capture: {len(raw)} bytes\n")

    # --- the rendering trap, stated as a test so nobody "simplifies" it back --
    flat = server._flat_pty(raw)
    check("de-ANSI'd modal text is space-free (ink pads with cursor motion)",
          "Resumefromsummary" in flat and "Resume from summary" not in flat,
          f"flat sample: {flat[-120:]!r}")
    check("a SPACED needle would match nothing (why _flat_pty strips whitespace)",
          "Resume from summary (recommended)" not in flat)

    # --- it fires on the real bytes ------------------------------------------
    s = fake_session()
    feed(s, raw)
    check("fires on the real modal, sending exactly one bare CR",
          s.sent == [b"\r"], f"sent={s.sent!r}")
    check("disarms itself after firing (one shot)", s._gate_deadline == 0.0)

    # A second identical burst must not fire again: a repaint of the same
    # screen (resize, refit) would otherwise send a stray CR into a live TUI.
    feed(s, raw)
    check("a repaint after firing sends nothing more", s.sent == [b"\r"])

    # --- chunk splitting: the modal never arrives in one read ----------------
    for size in (1, 7, 64, 512):
        s = fake_session()
        feed(s, raw, chunk=size)
        check(f"fires when the modal is split into {size}-byte chunks",
              s.sent == [b"\r"], f"sent={s.sent!r}")

    # --- it must NOT fire on prose that merely quotes the dialog -------------
    # No out-of-band oracle exists for this modal (claude's status file still
    # reads "idle" while it is up), so these two files — which describe the
    # dialog in full — are the realistic false-positive material.
    for path in ("server.py", "CLAUDE.md", os.path.basename(__file__)):
        p = os.path.join(HERE, path)
        if not os.path.exists(p):
            continue
        s = fake_session()
        feed(s, open(p, "rb").read(), chunk=4096)
        check(f"does not fire on {path} quoting the dialog", s.sent == [],
              f"sent={s.sent!r}")

    # --- arming policy --------------------------------------------------------
    s = fake_session(armed=False)          # a FRESH spawn never arms it
    feed(s, raw)
    check("a fresh (non-resume) spawn never fires", s.sent == [])

    s = fake_session(window=-1)            # window elapsed
    feed(s, raw)
    check("past the window it fires nothing and stops scanning",
          s.sent == [] and s._gate_deadline == 0.0)

    # --- engine fence ---------------------------------------------------------
    check("codex opts out (empty resume_gate_key)",
          server.CodexEngine.resume_gate_key == b"",
          f"got {server.CodexEngine.resume_gate_key!r}")
    check("claude answers with a bare CR",
          server.ClaudeEngine.resume_gate_key == b"\r")

    # --- the write() disarm ---------------------------------------------------
    # Any PTY write disarms the scan, so our CR can never land between a
    # harness send's text and its own submitting CR (posting half a prompt).
    import inspect
    body = inspect.getsource(server.ClaudeSession.write)
    check("write() disarms the gate scan", "_gate_deadline = 0.0" in body)

    print()
    if FAILED:
        print(f"FAILED ({len(FAILED)}): " + ", ".join(FAILED))
        return 1
    print("all resume-gate checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
