#!/usr/bin/env python3
"""The 📚 skill picker's server half: `skills_meta` + the skillsList frame.

The picker lists whatever is installed in ~/.claude/skills on the machine the
viewer is driving — repo-kit skills, fleet-synced ones (docs/fleet/SKILLS.md)
and hand-placed ones alike — and its auto-send points the session at the
SKILL.md path, so the path in the frame must be real and absolute. Pins:

  * every dir with a SKILL.md is listed, sorted, with its frontmatter
    description parsed (quoted or bare),
  * a skill without frontmatter still lists (empty description, no crash),
  * dot-dirs and dirs without SKILL.md are skipped,
  * a missing/empty skills root returns [] (fresh box, no error).

Temp dirs only — no SessionManager, nothing spawned.

    python3 test_skills_frame.py
"""
import sys
import tempfile
from pathlib import Path

import server

FAILS = []


def check(name, ok, detail=""):
    print(f"  {'ok' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        root = home / ".claude" / "skills"

        check("missing root → []", server.skills_meta(home) == [])

        (root / "vesta").mkdir(parents=True)
        (root / "vesta" / "SKILL.md").write_text(
            "---\nname: vesta\ndescription: \"put words on the Vestaboard\"\n---\nbody\n")
        (root / "bare").mkdir()
        (root / "bare" / "SKILL.md").write_text("no frontmatter at all\n")
        (root / ".hidden").mkdir()
        (root / ".hidden" / "SKILL.md").write_text("---\ndescription: nope\n---\n")
        (root / "not-a-skill").mkdir()
        (root / "not-a-skill" / "README.md").write_text("no SKILL.md here")

        got = server.skills_meta(home)
        names = [s["name"] for s in got]
        check("lists exactly the SKILL.md dirs, sorted", names == ["bare", "vesta"], str(names))
        vesta = next((s for s in got if s["name"] == "vesta"), {})
        check("frontmatter description parsed (quotes stripped)",
              vesta.get("description") == "put words on the Vestaboard")
        check("path is the absolute SKILL.md",
              vesta.get("path") == str(root / "vesta" / "SKILL.md"))
        bare = next((s for s in got if s["name"] == "bare"), {})
        check("no frontmatter → empty description, still listed",
              bare.get("description") == "")

        # the ✕ hidden set: UI-only, persisted, reversible (never touches files)
        hf = home / "hidden.json"
        check("fresh box → nothing hidden", server.skills_hidden(hf) == set())
        server.skills_hide("vesta", True, hf)
        server.skills_hide("bare", True, hf)
        server.skills_hide("bare", False, hf)
        check("hide persists, unhide reverses", server.skills_hidden(hf) == {"vesta"})
        check("hiding never touches the skill's files",
              (root / "vesta" / "SKILL.md").is_file())
        server.skills_hide("", True, hf)
        server.skills_hide("x" * 200, True, hf)
        check("empty/absurd names refused", server.skills_hidden(hf) == {"vesta"})
        hf.write_text("not json {{{")
        check("corrupt hidden file → empty set, no crash",
              server.skills_hidden(hf) == set())

    print("\nall skills-frame checks passed" if not FAILS else f"\nFAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
