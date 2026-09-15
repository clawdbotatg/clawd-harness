#!/usr/bin/env python3
"""test_stt_words.py — 🎤 the shared dictation word list on the relay.

One file (`stt-words.txt` on the doc shelf) that the harness page reads and
writes over HTTP (`/stt/words`, gated like /upload) and clawd-dictate / the
phone keyboard read with a fleet-docs credential. On a throwaway relay:
  1. no list yet → GET is 200 and empty (never a 404 the page has to special-case);
  2. POST replaces it, GET returns the same bytes, text/plain, no-store;
  3. the gate: a wrong token is 403 on both verbs;
  4. limits: an oversized body is 413, a non-UTF-8 body is 400 — and neither
     touches the stored list;
  5. it IS the doc shelf's file: a fleet-docs style read of `stt-words.txt`
     with a doc credential returns the same bytes.
Exits non-zero on any failure.
"""
import json, os, subprocess, sys, tempfile, time, urllib.request, urllib.error
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
PORT = "8809"
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "stt-words-test-token"
TMP = Path(tempfile.mkdtemp(prefix="stt-words-test-"))
CRED_FILE = TMP / "docs.credentials.json"
DOC_TOKEN = "docs-agent-token-for-test"

ENV = {
    **os.environ,
    "FLEET_PORT": PORT,
    "FLEET_BIND": "127.0.0.1",
    "FLEET_MOBILE_TOKEN": TOKEN,
    "FLEET_WORKER_TOKEN": "stt-words-test-worker",
    "FLEET_REQUIRE_PASSKEY": "0",     # the gate is the mobile token here (PASSKEY_ONLY off)
    "FLEET_PREFS_FILE": str(TMP / "prefs.json"),
    "FLEET_SESSIONS_FILE": str(TMP / "sessions.json"),
    "FLEET_PUSH_SUBS_FILE": str(TMP / "push.json"),
    "FLEET_DOCS_DIR": str(TMP / "docs"),
    "FLEET_DOCS_CREDENTIALS_FILE": str(CRED_FILE),
}


def http(method, path, body=None, headers=None):
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def main():
    # a doc credential so step 5 can read the shelf the way fleet-docs does
    try:
        sys.path.insert(0, str(HERE))
        import docs_store
        CRED_FILE.write_text(json.dumps(docs_store.Credentials.example(DOC_TOKEN)) if hasattr(docs_store.Credentials, "example") else "{}")
    except Exception:
        CRED_FILE.write_text("{}")
    proc = subprocess.Popen([sys.executable, "relay.py"], env=ENV, cwd=str(HERE),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.2)
    checks = []

    def check(name, cond, detail=""):
        checks.append((name, bool(cond)))
        print(f"  {'✓' if cond else '✗ FAIL'} {name}" + ("" if cond or not detail else f" — {detail}"))

    ok = f"/stt/words?t={quote(TOKEN)}"
    bad = "/stt/words?t=nope"
    try:
        # 1. empty before anything is saved
        st, hd, body = http("GET", ok)
        check("no list yet → 200 and empty", st == 200 and body == b"", f"{st} {body[:40]!r}")

        # 2. save, read back
        text = "Codex\nethskills\non chain => onchain\n".encode()
        st, hd, body = http("POST", ok, text, {"Content-Type": "text/plain; charset=utf-8"})
        check("POST saves the list", st == 200 and json.loads(body).get("ok") is True, f"{st} {body[:80]!r}")
        st, hd, body = http("GET", ok)
        check("GET returns the same bytes", st == 200 and body == text, f"{st} {body[:80]!r}")
        check("text/plain, no-store", hd.get("Content-Type", "").startswith("text/plain") and "no-store" in hd.get("Cache-Control", ""), str(hd))

        # 3. the gate
        st, _, _ = http("GET", bad)
        check("wrong token: GET is 403", st == 403, str(st))
        st, _, _ = http("POST", bad, b"x", {"Content-Type": "text/plain"})
        check("wrong token: POST is 403", st == 403, str(st))

        # 4. limits leave the stored list alone
        st, _, _ = http("POST", ok, b"a" * (64 * 1024 + 1), {"Content-Type": "text/plain"})
        check("oversized body is 413", st == 413, str(st))
        st, _, _ = http("POST", ok, b"\xff\xfe\xfd", {"Content-Type": "text/plain"})
        check("non-UTF-8 body is 400", st == 400, str(st))
        st, _, body = http("GET", ok)
        check("…and the list is untouched", body == text, body[:80].decode(errors="replace"))

        # 5. it's the doc shelf's file
        on_disk = list((TMP / "docs").rglob("stt-words.txt"))
        check("stored as stt-words.txt on the doc shelf", bool(on_disk) and on_disk[0].read_bytes() == text, str(on_disk))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    bad_checks = [n for n, c in checks if not c]
    print()
    if bad_checks:
        print(f"FAIL: {len(bad_checks)} check(s): " + "; ".join(bad_checks))
        sys.exit(1)
    print("PASS — one shared word list: page-gated over HTTP, the doc shelf's file underneath")


if __name__ == "__main__":
    main()
