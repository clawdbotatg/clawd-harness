#!/usr/bin/env python3
"""Re-benchmark the LLM-gateway models for the AI session-naming job.

Naming (server.generate_name) is a cheap, frequent, fire-and-forget labeler, so
the right model is the cheapest/fastest one that *reliably* emits clean JSON —
not a reasoning model. New models ship constantly, so **re-run this every few
months** and update BANKR_MODEL in .clawd-harness.env if something beats the
incumbent. See the "Right model for the right job" note in CLAUDE.md.

Reuses server.py's config (key/base/auth from .clawd-harness.env — no hardcoded
secret) and the exact NAME_SYS_PROMPT the app uses, so results can't drift.

Usage:
  python3 bench_naming.py                 # pull the full live model list, test all
  python3 bench_naming.py m1 m2 …         # test only the named models
Assumes an OpenAI-compatible gateway (/v1/chat/completions, /v1/models).
"""
import json, sys, time, re, urllib.request
import server   # importing only loads .clawd-harness.env + defines config; starts nothing

BASE = server.BANKR_BASE_URL
KEY  = server.BANKR_API_KEY
AUTH = ({"X-API-Key": KEY} if server.BANKR_API == "bankr"
        else {"Authorization": f"Bearer {KEY}"})

# Varied transcripts: coding, debugging, infra, and a deliberately off-task one
# (the haiku) to check the model labels what's actually happening, terse.
SAMPLES = [
    "User: add a swipe-to-dismiss gesture to the photo gallery\nClaude: I'll add "
    "touchstart/touchend handlers tracking horizontal delta and animate off-screen.",
    "User: the deploy keeps failing on vercel with a 404 on the api routes\nClaude: "
    "Usually a rewrites/output-dir mismatch — let me check vercel.json and the preset.",
    "User: refactor the auth middleware to use JWT refresh tokens\nClaude: I'll split "
    "access/refresh, add a rotation endpoint and httpOnly cookies.",
    "User: set up a github action to run pytest on every PR\nClaude: I'll add a "
    "workflow with a matrix over python versions and a pip cache.",
    "User: can you write a haiku about debugging\nClaude: Sure — here's one about the "
    "late-night hunt for a null pointer.",
]


def fetch_models():
    """Return (ordered id list, {id: pricing}) from the gateway's /v1/models,
    which embeds per-million-token pricing — so cost ranks alongside speed."""
    req = urllib.request.Request(f"{BASE}/models", headers=AUTH)
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode())
    items = data.get("data", data if isinstance(data, list) else [])
    ids = [m.get("id") for m in items if m.get("id")]
    price = {m["id"]: (m.get("pricing") or {}) for m in items if m.get("id")}
    return ids, price


def call(model, text):
    body = {"model": model, "max_tokens": 120, "temperature": 0.3,
            "messages": [{"role": "system", "content": server.NAME_SYS_PROMPT},
                         {"role": "user", "content": text}]}
    req = urllib.request.Request(
        f"{BASE}/chat/completions", data=json.dumps(body).encode(),
        headers={**AUTH, "content-type": "application/json"}, method="POST")
    t = time.time()
    with urllib.request.urlopen(req, timeout=40) as r:
        payload = json.loads(r.read().decode())
    ms = int((time.time() - t) * 1000)
    raw = (((payload.get("choices") or [{}])[0]).get("message") or {}).get("content", "")
    u = payload.get("usage") or {}
    m = re.search(r"\{[\s\S]*\}", raw)
    parsed = json.loads(m.group(0)) if m else None
    clean = bool(parsed) and raw.strip().startswith("{")
    return ms, parsed, clean, u.get("prompt_tokens", 0), u.get("completion_tokens", 0)


def cost_per_1k(price, ptok, ctok):
    """Est. USD to run 1,000 naming calls at the measured token usage. Reasoning
    models bill their hidden thinking as completion tokens, so this captures the
    'reasoning tax' that per-token sticker price alone hides."""
    if not price or not ptok:
        return None
    inp = price.get("input", 0); out = price.get("output", 0)
    return (ptok * inp + ctok * out) / 1e6 * 1000


def main():
    if not (KEY and BASE):
        print("No gateway creds — set BANKR_API_KEY + BANKR_BASE_URL in "
              ".clawd-harness.env"); return
    if sys.argv[1:]:
        models = sys.argv[1:]; _, price = fetch_models()
    else:
        models, price = fetch_models(); models = sorted(models)
    n = len(SAMPLES)
    print(f"benchmarking {len(models)} models × {n} samples "
          f"(via {BASE}, api={server.BANKR_API})\n")
    rows = []
    for model in models:
        times, ok, clean, ex, pt, ct = [], 0, 0, [], [], []
        for text in SAMPLES:
            try:
                ms, parsed, c, ptok, ctok = call(model, text)
                times.append(ms); pt.append(ptok); ct.append(ctok)
                if parsed: ok += 1; ex.append(parsed.get("title", "?"))
                if c: clean += 1
            except Exception as e:
                ex.append(f"ERR {str(e)[:50]}")
        p50 = sorted(times)[len(times) // 2] if times else 0
        avg_p = sum(pt) // len(pt) if pt else 0
        avg_c = sum(ct) // len(ct) if ct else 0
        cost = cost_per_1k(price.get(model), avg_p, avg_c)
        rows.append((model, ok, clean, p50, cost, avg_c, ex))
        cs = f"${cost:.3f}/1k" if cost is not None else "  n/a   "
        flag = "" if ok == n else "   ⚠ unreliable JSON"
        print(f"{model:<26} json={ok}/{n} clean={clean}/{n} p50={p50:>5}ms "
              f"cost={cs} out_tok={avg_c}{flag}")

    # Rank the JSON-reliable models by est. cost (the user's priority), showing
    # latency alongside. Cheapest reliable wins; latency breaks near-ties.
    good = sorted([r for r in rows if r[1] == n and r[4] is not None],
                  key=lambda r: (r[4], r[3]))
    cur = server.BANKR_MODEL
    print(f"\n=== ranked: reliable JSON, cheapest first (cost per 1,000 calls) ===")
    for model, ok, clean, p50, cost, avg_c, ex in good[:10]:
        mark = "  ← current" if model == cur else ""
        print(f"  {model:<26} ${cost:.3f}/1k  {p50:>5}ms  clean={clean}/{n}"
              f"  e.g. {ex[0][:28]}{mark}")
    if good:
        cheap = good[0]
        fast = min(good, key=lambda r: r[3])
        print(f"\ncheapest reliable : {cheap[0]}  (${cheap[4]:.3f}/1k, {cheap[3]}ms)")
        print(f"fastest reliable  : {fast[0]}  (${fast[4]:.3f}/1k, {fast[3]}ms)")
        print(f"current BANKR_MODEL: {cur}")


if __name__ == "__main__":
    main()
