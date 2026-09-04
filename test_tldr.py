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

print("tee_is_main_call:")
check("small side call is not the conversation", not server.tee_is_main_call(4000))
check("big body is the conversation", server.tee_is_main_call(server.API_TEE_MAIN_MIN))

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
want = [("block", ""), ("text", "Hello, "), ("text", "world.")]
tap = server.SseTextTap()
check("whole stream at once", tap.feed(stream) == want)
for n in (1, 7, 33):
    tap = server.SseTextTap(); got = []
    for i in range(0, len(stream), n):
        got += tap.feed(stream[i:i + n])
    check(f"same result chunked every {n} bytes", got == want)
tap = server.SseTextTap()
check("garbage that never ends an event yields nothing", tap.feed(b"data: {not json") == [])

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

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}"); raise SystemExit(1)
print("all ok")
