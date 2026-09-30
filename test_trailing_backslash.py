#!/usr/bin/env python3
"""A message ending in `\\` must still submit. claude's input reads `\\` +
Enter as "insert a newline", so `...let's print them\\` was typed, receipted
✓, and sat unsent until the next send's Enter carried both (Austin, 09-30).
send_message pads such a text with a space so the CR submits.

Binds the REAL ClaudeSession.send_message to a stub — no SessionManager, no
PTY, nothing spawns.

    python3 test_trailing_backslash.py
"""
import server

FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label
          + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(label)


class Eng:
    bracketed_paste = True
    def send_settle(self, big): return 0


class Stub:
    send_message = server.ClaudeSession.send_message
    def __init__(self):
        self.eng, self.hook_count, self.writes = Eng(), 0, []
    def ensure_started(self): pass
    def write(self, b): self.writes.append(b)
    def _send_watchdog(self, *a): pass


def sent(text, control=False):
    s = Stub()
    s.send_message(text, control=control)
    return s.writes


w = sent("let's print them\\")
check("trailing backslash gets a space before the CR",
      w[0] == b"\x1b[200~let's print them\\ \x1b[201~", w)
check("CR still follows", w[-1] == b"\r", w)
w = sent("plain message")
check("an ordinary message is untouched",
      w[0] == b"\x1b[200~plain message\x1b[201~", w)
w = sent("a\\b")
check("a backslash mid-text is untouched", w[0] == b"\x1b[200~a\\b\x1b[201~", w)
w = sent("/compact\\", control=True)
check("control sends are untouched", w[0] == b"/compact\\", w)

print("PASS" if not FAILED else f"{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
