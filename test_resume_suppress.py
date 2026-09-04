#!/usr/bin/env python3
"""Resume-modal suppression: don't answer the modal — don't let it paint.

The resume gate (test_resume_gate.py) answers the CLI's resume modal with the
preselected choice, which runs /compact: a full-context model turn billed to
whatever pool the session just resumed onto. At handoff scale that is the
dominant per-move token cost (the --resume replay itself is local and free).

The CLI only paints the modal when the session's age clears an env-tunable
floor (CLAUDE_CODE_RESUME_THRESHOLD_MINUTES ?? 70, verified in the 2.1.235
bundle), so the harness pins a huge floor into every claude child's env:
no modal, no auto-/compact, near-free handoffs. The gate scan stays armed as
the backstop for a CLI that ignores the knob.

These tests pin the env contract without spawning anything:

    python3 test_resume_suppress.py
"""
import os
import tempfile
import types

import server

FAILED = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def fake_session(tmp):
    """Only what ClaudeEngine.env touches. config_dir is an empty temp dir —
    no .claude.json — so the onboarding/trust seeders decline untouched."""
    cfg = os.path.join(tmp, "acct")
    os.makedirs(cfg, exist_ok=True)
    return types.SimpleNamespace(cid="test-session", config_dir=cfg,
                                 workdir=lambda: tmp)


def child_env(s):
    env = {}
    server.ClaudeEngine().env(s, env)
    return env


def main():
    with tempfile.TemporaryDirectory() as tmp:
        s = fake_session(tmp)

        env = child_env(s)
        check("default: minutes floor pinned in the child env",
              env.get("CLAUDE_CODE_RESUME_THRESHOLD_MINUTES") == server.RESUME_MODAL_FLOOR_MIN,
              repr(env.get("CLAUDE_CODE_RESUME_THRESHOLD_MINUTES")))
        check("floor is a huge number of minutes (a year, not a tweak)",
              float(server.RESUME_MODAL_FLOOR_MIN) >= 525600)

        env = {"CLAUDE_CODE_RESUME_THRESHOLD_MINUTES": "45"}
        server.ClaudeEngine().env(s, env)
        check("an operator export wins over the pin (setdefault)",
              env["CLAUDE_CODE_RESUME_THRESHOLD_MINUTES"] == "45")

        old = server.RESUME_MODAL_SUPPRESS
        try:
            server.RESUME_MODAL_SUPPRESS = False
            env = child_env(s)
            check("RESUME_MODAL_SUPPRESS=0 leaves the child env alone",
                  "CLAUDE_CODE_RESUME_THRESHOLD_MINUTES" not in env)
        finally:
            server.RESUME_MODAL_SUPPRESS = old

        # Suppression is belt; the gate scan is suspenders. Neither may
        # disable the other: the scan must stay wired for the day the CLI
        # ignores the undocumented knob.
        check("gate scan backstop still armed by default",
              server.RESUME_GATE is True)
        check("claude engine still answers the modal with a bare CR",
              server.ClaudeEngine.resume_gate_key == b"\r")

        env = child_env(s)
        check("alt-screen pin untouched by the new line",
              env.get("CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN") == "1")

        # The knob must survive the child-env scrub, or none of this ships.
        check("threshold var is not scrubbed from child envs",
              "CLAUDE_CODE_RESUME_THRESHOLD_MINUTES" not in server.SCRUB_ENV)

    if FAILED:
        print(f"\n{len(FAILED)} FAILED: {FAILED}")
        raise SystemExit(1)
    print("\nall 8 checks passed")


if __name__ == "__main__":
    main()
