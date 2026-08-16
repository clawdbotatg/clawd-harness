#!/usr/bin/env python3
"""Re-mine the quick-prompt chip ranking (QUICK_PROMPTS in index.html).

Counts how often each short prompt is actually sent, from two sources:
  1. .clawd-harness.prompts.jsonl — the harness's first-party send log
     (written by log_prompt in server.py; `via:"quick"` = a chip tap,
     which is reported separately so chips don't self-reinforce).
  2. ~/.claude/projects/*/*.jsonl — the shared Claude Code transcript
     store (backfill; includes CLI sessions the harness never saw).

Run it, eyeball the ranking, then reorder/extend the QUICK_PROMPTS array
by hand (most→least used: the FIRST entry renders farthest left).
Cadence: every few months — see CLAUDE.md.

Usage: python3 tools/mine_quick_prompts.py [--max-len 60] [--top 40]
"""
import argparse
import collections
import glob
import json
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_LOG = os.path.join(HERE, ".clawd-harness.prompts.jsonl")
TRANSCRIPT_STORE = os.path.expanduser("~/.claude/projects")

# Known noise: harness smoke tests, CLI interrupt markers.
NOISE = re.compile(r"^(reply with the single word ok|\[request interrupted)", re.I)


def norm(s):
    s = re.sub(r"\s+", " ", s.strip().lower())
    return s.rstrip(".!?, ")


def mine_send_log(counts, chip_taps, max_len):
    n = 0
    try:
        with open(PROMPTS_LOG, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                t = (rec.get("text") or "").strip()
                if not t or len(t) > max_len or NOISE.match(t):
                    continue
                if rec.get("via") == "auto":
                    continue        # harness-fired (auto-tldr), not the human speaking
                n += 1
                if rec.get("via") == "quick":
                    chip_taps[norm(t)] += 1     # counted apart: taps ≠ organic typing
                else:
                    counts[norm(t)] += 1
    except FileNotFoundError:
        pass
    return n


def mine_transcripts(counts, max_len):
    n = 0
    for path in glob.glob(os.path.join(TRANSCRIPT_STORE, "*", "*.jsonl")):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("type") != "user" or obj.get("isMeta"):
                        continue
                    c = (obj.get("message") or {}).get("content")
                    if isinstance(c, list):
                        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                            continue
                        c = " ".join(b.get("text", "") for b in c
                                     if isinstance(b, dict) and b.get("type") == "text")
                    if not isinstance(c, str):
                        continue
                    t = c.strip()
                    if (not t or len(t) > max_len or t.startswith("<")
                            or t.startswith("Caveat:") or NOISE.match(t)):
                        continue
                    n += 1
                    counts[norm(t)] += 1
        except OSError:
            pass
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-len", type=int, default=60,
                    help="only prompts this short are chip candidates")
    ap.add_argument("--top", type=int, default=40)
    args = ap.parse_args()

    counts, chip_taps = collections.Counter(), collections.Counter()
    n_log = mine_send_log(counts, chip_taps, args.max_len)
    n_tr = mine_transcripts(counts, args.max_len)
    print(f"short prompts mined: {n_log} from send log, {n_tr} from transcripts\n")

    print(f"top {args.top} typed short prompts (chip candidates, most-used first):")
    for t, c in counts.most_common(args.top):
        print(f"{c:5d}  {t!r}")

    if chip_taps:
        print("\nchip taps (via:'quick' — proof a chip earns its spot):")
        for t, c in chip_taps.most_common():
            print(f"{c:5d}  {t!r}")


if __name__ == "__main__":
    main()
