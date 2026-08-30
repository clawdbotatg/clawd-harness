#!/usr/bin/env python3
"""Parity: the browser's iron sanitizer (index.html [irons-sanitizer]) and the
relay's clean_irons (relay.py) must agree byte-for-byte on EVERY input.

The fleet client fingerprints its own list through its sanitizer to recognize
the relay's echo as its ack — any divergence (a cap one side missed, a slice
counting UTF-16 units instead of code points) makes the echo never match, and
since each re-push resets nothing, that's an infinite push/echo loop hammering
the prefs file and every device. This test feeds adversarial vectors to both
copies and diffs the results; it also asserts the sanitizer is idempotent
(clean(clean(x)) == clean(x)), which is what lets a locally-normalized list
match its own echo trivially.

Requires `node` on PATH (like test_e2e_interop). Exits non-zero on mismatch.
"""
import json
import os
import subprocess
import sys
import tempfile

import relay

HERE = os.path.dirname(os.path.abspath(__file__))

NODE_RUNNER = r"""
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const core = html.split('// [irons-sanitizer-begin]')[1].split('// [irons-sanitizer-end]')[0];
eval(core);
const vectors = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
process.stdout.write(JSON.stringify(vectors.map(v => cleanIrons(v))));
"""


def vectors():
    emoji_title = "🔥💧" * 50                 # 100 astral code points — clips at 80 CODE POINTS, not UTF-16 units
    return [
        None,
        "junk-string",                        # a string must NOT iterate as characters
        {"id": "x", "title": "dict-not-list"},
        42,
        [],
        [{"id": "a", "title": "plain", "keys": ["k"], "tags": ["t"], "created": 5}],
        [   # junk entries, dup ids, blank titles, wrong-typed fields
            {"id": "ok1", "title": "  keep  ", "tags": "abc", "keys": "abcd", "created": True},
            {"id": "ok1", "title": "dupe"},
            {"id": "", "title": "no id"},
            {"id": "ok2", "title": "   "},
            {"id": "ok3", "title": "t", "tags": ["a", 7, "  ", "b" * 100],
             "keys": ["k1", 9, {"x": 1}, "", "k" * 600], "desc": 12, "created": 1.5},
            "not-a-dict", 42, None, ["list-entry"],
            {"id": "i" * 65, "title": "id too long"},
        ],
        [{"id": "emoji", "title": emoji_title, "desc": emoji_title * 5,
          "tags": [emoji_title], "keys": ["🔥" * 513, "🔥" * 512]}],
        [{"id": f"i{n}", "title": f"t{n}"} for n in range(70)],   # 64-cap
        [{"id": "ws", "title": "  padded  ", "desc": "d e",
          "tags": ["　tag　"], "keys": ["k"], "created": -3}],
    ]


def main():
    vecs = vectors()
    py = [relay.clean_irons(v) for v in vecs]

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(NODE_RUNNER.replace("__dirname", json.dumps(HERE)))
        runner = f.name
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(vecs, f)
        vpath = f.name
    try:
        out = subprocess.run(["node", runner, vpath],
                             capture_output=True, text=True)
    finally:
        os.unlink(vpath)
        os.unlink(runner)
    if out.returncode != 0:
        print("node runner failed:\n", out.stderr)
        sys.exit(1)
    js = json.loads(out.stdout)

    fails = []
    for n, (p, j) in enumerate(zip(py, js)):
        if p != j:
            fails.append(f"vector {n}:\n  py={json.dumps(p, ensure_ascii=False)}\n"
                         f"  js={json.dumps(j, ensure_ascii=False)}")
    if not fails:
        print(f"  ✓ {len(vecs)} vectors sanitize identically (py ↔ js)")

    for n, p in enumerate(py):
        if relay.clean_irons(p) != p:
            fails.append(f"vector {n}: clean_irons is not idempotent")
    if not fails:
        print("  ✓ sanitizer is idempotent (echo of a normalized list is a fixpoint)")

    if fails:
        print("FAILED:\n" + "\n".join(fails))
        sys.exit(1)
    print("PASSED: irons sanitizer parity")


if __name__ == "__main__":
    main()
