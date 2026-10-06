#!/usr/bin/env python3
"""Install Claude setup-tokens into this box's harness.

Copies every CLAUDE_TOKEN_<NAME>=sk-ant-oat01-… line from a file (the
claude-tokens skill) into the harness's gitignored .clawd-harness.env,
replacing older values. Saving that file restarts the harness gracefully;
it then runs one token account per line (docs/fleet/SUB-ROUTING.md §
Token accounts). Prints names only, never a token.

  python3 tools/install_claude_tokens.py <path-to-claude-tokens-SKILL.md>
"""
import os
import re
import sys
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".clawd-harness.env"
LINE = re.compile(r"^(CLAUDE_TOKEN_[A-Z0-9_]+)=(sk-ant-oat01-[A-Za-z0-9_-]+)\s*$")


def merge(env_text, tokens):
    """env_text with each token set (existing keys replaced in place, new
    ones appended). Returns (new_text, changed_names)."""
    lines, seen, changed = env_text.splitlines(), set(), []
    for i, line in enumerate(lines):
        key = line.partition("=")[0].strip()
        if key in tokens:
            seen.add(key)
            if line.strip() != f"{key}={tokens[key]}":
                lines[i] = f"{key}={tokens[key]}"
                changed.append(key)
    for key, val in tokens.items():
        if key not in seen:
            lines.append(f"{key}={val}")
            changed.append(key)
    return "\n".join(lines) + "\n", changed


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__)
    tokens = {}
    for line in Path(argv[1]).read_text().splitlines():
        m = LINE.match(line.strip())
        if m:
            tokens[m.group(1)] = m.group(2)
    if not tokens:
        sys.exit(f"no CLAUDE_TOKEN_<NAME>=sk-ant-oat01-… lines in {argv[1]}")
    old = ENV_FILE.read_text() if ENV_FILE.exists() else ""
    new, changed = merge(old, tokens)
    if not changed:
        print(f"already installed: {', '.join(tokens)} — nothing to do")
        return
    tmp = ENV_FILE.with_name(ENV_FILE.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(new)
    os.replace(tmp, ENV_FILE)
    os.chmod(ENV_FILE, 0o600)
    print(f"installed {', '.join(changed)} into {ENV_FILE}")
    print("the harness restarts itself once in-flight turns finish; "
          "every session resumes")


if __name__ == "__main__":
    main(sys.argv)
