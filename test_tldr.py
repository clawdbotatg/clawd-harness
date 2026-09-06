#!/usr/bin/env python3
"""🟦 live TLDR guards (2026-09-04): the pure pieces of the API tee + rolling
summarizer. Pins (1) per-session routing off the URL path, (2) the main-call
vs side-call gate, (3) SSE text extraction under arbitrary chunking, and
(4) the RollingTldr loop contract — one call in flight, every call sees the
whole text so far, newest wins, the finished-text pass is final and ends the
loop, a stop() abandons a stale in-flight result.

Pure helpers + an injected fake runner: constructs no SessionManager, spawns
nothing, opens no port.

    python3 test_tldr.py
"""
import json, threading, time
import server

FAILS = []


def check(name, cond):
    print(("  ok  " if cond else "  FAIL") + " " + name)
    if not cond:
        FAILS.append(name)


print("tee_upstream_path:")
check("strips /s/<cid> and keeps the query",
      server.tee_upstream_path("/s/abc123/v1/messages?beta=true")
      == ("abc123", "/v1/messages?beta=true"))
check("no prefix → cid None, path untouched",
      server.tee_upstream_path("/v1/messages") == (None, "/v1/messages"))
check("deep path survives",
      server.tee_upstream_path("/s/c/v1/messages/count_tokens")
      == ("c", "/v1/messages/count_tokens"))

print("tee_is_ours:")
check("a tee url is ours", server.tee_is_ours("http://127.0.0.1:8791/s/abc-123"))
check("a gateway is not", not server.tee_is_ours("https://llm.example.com/v1"))
check("empty is not", not server.tee_is_ours(""))

print("tee_is_subagent:")
main = json.dumps({"model": "m", "system": [{"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.257.4c4; cc_entrypoint=cli;"}, {"type": "text", "text": "You are Claude Code"}], "messages": []}).encode()
sub = json.dumps({"model": "m", "system": [{"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.257.2b6; cc_entrypoint=cli; cc_is_subagent=true;"}], "messages": []}).encode()
quoted = json.dumps({"model": "m", "system": [{"type": "text", "text": "x-anthropic-billing-header: cc_version=1; cc_entrypoint=cli;"}], "messages": [{"role": "user", "content": "the log said cc_is_subagent=true"}]}).encode()
check("main conversation is not a subagent", not server.tee_is_subagent(main))
check("Agent-tool call is a subagent", server.tee_is_subagent(sub))
check("marker quoted in a message doesn't count", not server.tee_is_subagent(quoted))
check("unparseable body: raw search", server.tee_is_subagent(b"garbage cc_is_subagent=true"))

print("tee_call_kind:")
big_pad = "x" * server.API_TEE_MAIN_MIN
conv = json.dumps({"system": [{"type": "text", "text": "x-anthropic-billing-header: cc_entrypoint=cli;"}], "tools": [{"name": "Bash"}], "messages": [{"role": "user", "content": big_pad}]}).encode()
fetch = json.dumps({"system": "You summarize web pages", "messages": [{"role": "user", "content": "\nWeb page content:\n---\n" + big_pad}]}).encode()
small = json.dumps({"tools": [{"name": "Bash"}], "messages": [{"role": "user", "content": "hi"}]}).encode()
subb = json.dumps({"system": [{"type": "text", "text": "x-anthropic-billing-header: cc_entrypoint=cli; cc_is_subagent=true;"}], "tools": [{"name": "Bash"}], "messages": [{"role": "user", "content": big_pad}]}).encode()
check("the conversation (big, has tools) is main", server.tee_call_kind(conv) == "main")
check("WebFetch's page summarizer (big, NO tools) is a side call", server.tee_call_kind(fetch) == "side")
check("a small call is a side call even with tools", server.tee_call_kind(small) == "side")
check("an Agent subagent is a subagent", server.tee_call_kind(subb) == "subagent")
check("tee_is_main_call takes the body", server.tee_is_main_call(conv) and not server.tee_is_main_call(fetch))

print("SseTextTap:")
def sse(*events):
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)
stream = sse({"type": "message_start"},
             {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
             {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}},
             {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
             {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hello, "}},
             {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "world."}},
             {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "name": "Bash"}},
             {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
             {"type": "message_stop"})
want = [("block", ""), ("text", "Hello, "), ("text", "world."), ("tool", "")]
tap = server.SseTextTap()
check("whole stream at once", tap.feed(stream) == want)
check("a tool_use block start emits ('tool','') so the tee can drop narration", ("tool", "") in want)
for n in (1, 7, 33):
    tap = server.SseTextTap(); got = []
    for i in range(0, len(stream), n):
        got += tap.feed(stream[i:i + n])
    check(f"same result chunked every {n} bytes", got == want)
tap = server.SseTextTap()
check("garbage that never ends an event yields nothing", tap.feed(b"data: {not json") == [])

print("tee_text drops tool-call narration:")
# tee_text is a Session method, but it only touches TLDR state. Bind it to a
# stand-in with those fields — no PTY, no port, no summarizer spawned. The
# rule under test: only prose with NO tool after it (the real reply) is kept.
class _FakeSess: pass
def _fake(on):
    f = _FakeSess()
    f._tldr_lock = threading.Lock()
    f.tldr_turn_text = f.tldr_text = ""
    f.tldr_read_at = 0
    f.tldr_sents = []
    f._tldr = None
    f._voice_said = ["stale"]
    f.tldr_on = on
    f._tldr_timer = None
    f._tldr_deadline = 0.0
    f.frames = []
    f._to_subscribers_json = lambda d: f.frames.append(d)
    f._tldr_frame = lambda summary, final, sents=None: {"type": "tldr", "text": summary}
    for m in ("tee_text", "_tldr_arm_timer_locked", "_tldr_cancel_timer", "_tldr_debounced"):
        setattr(f, m, getattr(server.ClaudeSession, m).__get__(f, _FakeSess))
    return f

f = _fake(False)                                   # tldr off: isolate the reset, no timer/summarizer
f.tee_text("text", "Let me check the file real quick.")
f.tee_text("tool", "")
check("a tool event drops the narration buffer", f.tldr_turn_text == "")
check("a tool event blanks the summary", f.tldr_text == "")
check("a tool event resets the voice said-log", f._voice_said == [])
check("a tool event pushes a blank blue frame",
      any(fr.get("type") == "tldr" and fr.get("text") == "" for fr in f.frames))

f = _fake(False)
f.tee_text("text", "narration"); f.tee_text("tool", "")
f.tee_text("text", "The real answer is 42.")
check("prose after the last tool is kept", f.tldr_turn_text == "The real answer is 42.")

f = _fake(False)
f.tee_text("text", "Just a normal reply, no tools.")
check("a tool-free reply is untouched", f.tldr_turn_text == "Just a normal reply, no tools.")

f = _fake(True)                                    # tldr on: text arms the debounce, tool cancels it
f.tee_text("text", "x" * 50)
armed = f._tldr_timer is not None
f.tee_text("tool", "")
check("text arms the debounce and a following tool cancels it (no summarizer spawned)",
      armed and f._tldr_timer is None)

print("tldr_budget:")
check("short reply → floor", server.tldr_budget("a b c", False) == 15 and server.tldr_budget("a b c", True) == 12)
check("600-word reply → 100 capped to 60 live / 50 final",
      server.tldr_budget("w " * 600, False) == 60 and server.tldr_budget("w " * 600, True) == 50)
check("180-word reply → 30 live, 22 final",
      server.tldr_budget("w " * 180, False) == 30 and server.tldr_budget("w " * 180, True) == 22)
long = "This is a sentence. " * 30                     # 120 words, sentence every 4
check("tidy cuts a long-running summary at a sentence end inside 1.4× budget",
      len(server.tldr_tidy(long, True, 20).split()) <= 28 and server.tldr_tidy(long, True, 20).endswith("."))
check("tidy leaves a within-budget summary alone", server.tldr_tidy("Short. Done.", True, 20) == "Short. Done.")

print("tldr_tidy:")
check("finished sentence untouched", server.tldr_tidy("Fixed. Tests pass.", False) == "Fixed. Tests pass.")
check("ragged tail dropped on a live pass", server.tldr_tidy("Fixed. Tests pass. Catches boomed t", False) == "Fixed. Tests pass.")
check("final pass never trims", server.tldr_tidy("Fixed. Tests pass. Catches boomed t", True) == "Fixed. Tests pass. Catches boomed t")
check("no finished sentence yet → keep what we have", server.tldr_tidy("Colonial settlers used lobster as", False) == "Colonial settlers used lobster as")
check("closing quote/paren after the period counts", server.tldr_tidy('He said "done." Then it', False) == 'He said "done."')

print("RollingTldr:")
calls, emitted = [], []
gate = threading.Event()          # runner blocks until released → proves one-in-flight
def runner(text, prev, final):
    calls.append((text, prev, final))
    gate.wait(2)
    return f"S{len(calls)}[{len(text)}{'F' if final else ''}]"
r = server.RollingTldr(lambda s, f: emitted.append((s, f)), runner)
r.feed("a" * 10)
time.sleep(0.15)
r.feed("a" * 20); r.feed("a" * 30)          # arrive while call 1 is in flight
time.sleep(0.15)
check("only one call in flight", len(calls) == 1)
gate.set(); time.sleep(0.3)
check("next call sees the NEWEST whole text (coalesced)",
      len(calls) == 2 and len(calls[1][0]) == 30 and calls[1][1] == "S1[10]")
check("every pass emitted, not final", emitted == [("S1[10]", False), ("S2[30]", False)])
r.feed("a" * 40); r.done()                   # Stop lands with fresh text still unread
time.sleep(0.3)
check("finish = one more pass over the complete text, flagged final",
      emitted[-1] == ("S3[40F]", True) and calls[-1][2] is True)
time.sleep(0.2)
check("loop exits after the final pass", not r.thread.is_alive())
check("no extra calls after final", len(calls) == 3)

calls.clear(); emitted.clear(); gate.clear()
r2 = server.RollingTldr(lambda s, f: emitted.append((s, f)), runner)
r2.feed("b" * 200); time.sleep(0.15)
r2.stop(); gate.set(); time.sleep(0.3)
check("stop() drops the in-flight result (a new prompt raced it)", emitted == [])
check("stopped loop exits", not r2.thread.is_alive())

def boom(text, prev, final):
    raise RuntimeError("claude -p died")
r3 = server.RollingTldr(lambda s, f: emitted.append((s, f)), boom)
r3.feed("c" * 50); r3.done(); time.sleep(0.3)
check("a failed pass keeps the previous summary and doesn't crash the loop",
      emitted == [] and not r3.thread.is_alive())

print("split/settle_sentences:")
check("splits on sentence ends and newlines",
      server.split_sentences("Fixed it. Tests pass!\nWant me to push?") == ["Fixed it.", "Tests pass!", "Want me to push?"])
check("first pass: nothing settled",
      server.settle_sentences([], "Fixed it. Tests pass.") == [("Fixed it.", False), ("Tests pass.", False)])
check("a sentence that survives verbatim settles; a reworded one doesn't",
      server.settle_sentences(["Fixed it.", "Tests pass."], "Fixed it. Tests pass now. Pushed.")
      == [("Fixed it.", True), ("Tests pass now.", False), ("Pushed.", False)])

print("voice_pick (the voice reads the blue text as it solidifies):")
said = []
p1 = server.voice_pick(said, [("Found the bug.", False), ("Tests pass.", False)], False)
check("nothing settled yet → nothing read", p1 == [])
p2 = server.voice_pick(said, [("Found the bug.", True), ("Tests pass.", False)], False)
check("a settled sentence is read", p2 == ["Found the bug."]); said += p2
p3 = server.voice_pick(said, [("Found the bug.", True), ("Tests pass.", True)], False)
check("already-read sentences are not read again", p3 == ["Tests pass."]); said += p3
p4 = server.voice_pick(said, [("Found the bug in the parser.", True)], False)
check("a reworded near-twin of something read is skipped", p4 == [])
p5 = server.voice_pick(said, [("Found the bug.", True), ("Pushed. Want a PR?", False)], True)
check("final reads what is left, settled or not", p5 == ["Pushed. Want a PR?"])
check("voice_pick never mutates the said-log", said == ["Found the bug.", "Tests pass."])

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}"); raise SystemExit(1)
print("all ok")
