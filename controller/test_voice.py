#!/usr/bin/env python3
"""Tests for the 🎙 voice PM's server half (controller/voice.py + endpoints).

What must hold (each learned from the gpt-voice reference's trap list or from
this repo's own history):
- The session config carries transcription (trap #3: omitting it silently kills
  user transcripts), semantic VAD, a realtime-legal voice, and every tool.
- Every declared tool has an exec spec the browser can dispatch on, and the
  spec kinds are exactly the three the client implements.
- read_lore never escapes LORE_DIR (basename-only) and bounds its output.
- /api/voice/token answers 503, not a traceback, when no key is configured.

Run:  python3 -m controller.test_voice
"""
import json
import os
import tempfile
import urllib.request

from . import voice


class FakeVerbs:
    def get_world(self):
        return {"machines": [{"id": "testbox", "connected": True, "sessions": 2}],
                "attention_count": 1}


def test_session_config_shape():
    cfg = voice.session_config(FakeVerbs())["session"]
    assert cfg["type"] == "realtime"
    audio = cfg["audio"]
    assert audio["input"]["turn_detection"]["type"] == "semantic_vad"
    assert audio["input"]["transcription"]["model"], "transcription is opt-in — must be present"
    assert audio["output"]["voice"] in ("marin", "cedar", "alloy", "echo",
                                        "sage", "shimmer", "verse"), \
        f"{audio['output']['voice']} is not a realtime voice (TTS-only names fail the mint)"
    names = [t["name"] for t in cfg["tools"]]
    assert "ask_pm" in names and "whats_waiting" in names
    assert all(t["type"] == "function" and t["parameters"]["type"] == "object"
               for t in cfg["tools"])
    # the live fleet snapshot made it into the persona
    assert "testbox" in cfg["instructions"]
    print(f"ok session_config ({len(names)} tools, "
          f"{len(cfg['instructions'])}B instructions)")


def test_exec_map_covers_tools():
    defs = {t["name"] for t in voice.tool_defs()}
    ex = voice.exec_map()
    assert set(ex) == defs, "every voice tool needs a client exec spec"
    kinds = {s["kind"] for s in ex.values()}
    assert kinds <= {"verb", "chat", "lore"}, f"client only dispatches verb/chat/lore, got {kinds}"
    assert all("name" in s for s in ex.values() if s["kind"] == "verb")
    print(f"ok exec_map ({sorted(kinds)})")


def test_lore_sandbox():
    old = voice.LORE_DIR
    with tempfile.TemporaryDirectory() as d:
        voice.LORE_DIR = d
        try:
            with open(os.path.join(d, "soul.md"), "w") as f:
                f.write("I am clawd. " * 4000)          # > LORE_MAX
            with open(os.path.join(d, "secrets.txt"), "w") as f:
                f.write("not lore")
            assert voice.lore_index() == ["soul"]        # .md only
            got = voice.read_lore("soul")
            assert got["text"].endswith("…(truncated)")
            assert len(got["text"]) <= voice.LORE_MAX + 20
            assert voice.read_lore("index") == {"pages": ["soul"]}
            # traversal / non-page names never read outside the dir
            for evil in ("../../etc/passwd", "/etc/passwd", "secrets.txt", "secrets"):
                assert "text" not in voice.read_lore(evil), evil
        finally:
            voice.LORE_DIR = old
    # missing dir degrades, never raises
    voice.LORE_DIR = "/nonexistent-lore-dir"
    try:
        assert voice.lore_index() == []
        assert "clawd" in voice._identity_brief().lower()
    finally:
        voice.LORE_DIR = old
    print("ok lore sandbox")


def test_token_endpoint_without_key():
    """chat_server answers 503 (clean JSON) when OPENAI_API_KEY is unset."""
    from .chat_server import ThreadingHTTPServer, make_handler
    old_key = voice.API_KEY
    voice.API_KEY = ""
    class G: autonomy = "readonly"
    class R:
        def list_threads(self): return {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0),
                              make_handler(R(), FakeVerbs(), G(), lambda: "test"))
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/voice/token",
                                     data=b"{}", method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("expected 503")
        except urllib.error.HTTPError as e:
            assert e.code == 503
            body = json.loads(e.read().decode())
            assert "OPENAI_API_KEY" in body["error"]
        # lore endpoint stays up regardless of the key
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/voice/lore?name=index", timeout=5) as r:
            assert "pages" in json.loads(r.read().decode())
    finally:
        voice.API_KEY = old_key
        srv.shutdown()
    print("ok /api/voice/token 503 without key, lore endpoint up")


import urllib.error  # noqa: E402  (used in the endpoint test)

if __name__ == "__main__":
    test_session_config_shape()
    test_exec_map_covers_tools()
    test_lore_sandbox()
    test_token_endpoint_without_key()
    print("voice: all green")
