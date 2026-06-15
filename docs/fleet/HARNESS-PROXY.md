# The harness-proxy worker  ✅ SHIPPED (2026-06)

> **Status: done and live.** This was the spec; the worker now implements it.
> The build followed this plan closely, with two deliberate choices: we went
> **straight to raw binary PTY framing** (option (b) below — no base64 stage), and
> the smoke test uses an **embedded mock harness** (`fleet_proxy_smoke.py`) rather
> than spinning a real `claude`. The mobile UI (the "separate, after the worker"
> section) also shipped — `index.html`, a fork of the harness page. Verified live
> through `wss://h.atg.link` against a real laptop harness. Code: `worker.py`
> (`HarnessLink`, `hsend`-equivalent dispatch), `relay.py` (binary routing,
> `mobileGone`), `index.html`. The text below is kept as the design record.

---

This was the single most important task — it turns the fleet from "run shell
commands on remote machines" into **"drive real Claude Code sessions on remote
machines."** Written so another Claude instance can pick it up cold.

Read first: [`ARCHITECTURE.md`](ARCHITECTURE.md) (the layering invariant) and the
harness's **`docs/WS-PROTOCOL.md`** (the exact contract — this whole task is
"bridge that protocol to the relay"). Living in `../clawd-harness/docs/WS-PROTOCOL.md`.

## Goal

Replace `worker.py`'s prototype `exec` handler with a **proxy** that:
1. runs on a machine alongside a clawd-harness (`server.py` on `127.0.0.1:8787`),
2. connects to that harness as a **client** (speaking its WebSocket protocol),
3. tunnels harness frames ⇄ relay, so a phone gets the harness's *own* UI
   (projects → sessions → transcript → tty) for that remote machine.

**Invariant (do not break):** clawd-harness is not modified. The worker is a
client of it, exactly like a browser. If you feel the urge to edit `server.py`,
stop — the fix belongs in the worker or relay.

## Why this is mostly plumbing, not new features

The harness protocol is already multi-project / multi-session, and its UI is
already the swipe stack `projects → sessions → transcript → tty`. The fleet adds
one rung on top (`machines`) and **tunnels the rest verbatim**. So the work is:
move bytes/JSON faithfully between two WebSockets, keyed by machine + viewer.

## Design

### Connection fan-out (the key subtlety)

A harness client is subscribed to **one session at a time** (`client.cid`). So to
let two phones view two different sessions on the same machine, the proxy opens
**one harness WS connection per remote viewer (per mobile_id)**:

```
 relay ──task(from=m1)──▶ worker ── harness-conn for m1 ──▶ harness :8787
 relay ──task(from=m2)──▶ worker ── harness-conn for m2 ──▶ harness :8787
                          (lazily created on first frame from that mobile;
                           torn down when the mobile disconnects)
```

Map: `mobile_id → HarnessConn`. Each `HarnessConn` is a `fleet_ws.client_connect`
to `ws://127.0.0.1:8787/ws?t=<harness token>` plus a reader thread.

### Frame bridging (both directions, both opcodes)

- **mobile → harness:** relay delivers `{type:"task", from:m, msg:<harnessFrame>}`.
  The worker takes `msg` (a harness control frame like `{type:"send",cid,text}`
  or `{type:"subscribe",cid}` or `{type:"input",...}`) and writes it to m's
  HarnessConn as a **masked text** frame.
- **harness → mobile:** the HarnessConn reader gets harness frames and wraps each
  back to the relay as `{type:"reply", to:m, msg:<harnessFrame>}`.
  - **text frames** (JSON: `projects`/`sessions`/`hello`/`transcript`/`hook`/…) →
    wrap as-is.
  - **binary frames** (PTY bytes) → must also reach the phone. Two options:
    - (a) base64 the bytes into a JSON `reply` (simplest, ~33% overhead), or
    - (b) **extend the relay to route raw binary frames** tagged by machine +
      mobile (cleaner, no bloat). Recommended — see "Relay changes" below.

### Auth

The worker reads the **harness** token locally from
`../clawd-harness/.clawd-harness.token` (or `CONSOLE_TOKEN`) to open the harness
WS. The **fleet** token (relay auth) stays as today. These are different secrets
for different hops — keep them separate.

### Machine roster vs. harness presence

The relay roster already lists machines. Optionally have the worker forward an
initial harness `projects`/`sessions` snapshot as a `status` so the phone can
show per-machine session counts without subscribing.

## Relay changes (small)

Today the relay routes JSON only. Add binary routing for PTY:
- Accept **binary** frames from workers/mobiles (opcode `0x2`) carrying a tiny
  header (e.g. first bytes = `mobile_id`/`machine_id` + `cid`) or — simpler —
  keep PTY inside JSON `reply`/`task` via base64 to start, and optimize later.
- The existing `from_worker`/`from_mobile` routing and `mobile_id`/`machine_id`
  maps are the right structure; you're adding a second payload type, not new
  routing.

Recommended staging: **ship with base64-in-JSON first** (no relay protocol
change, fastest path to a working remote terminal), then optimize to raw binary
framing once it works.

## Mobile UI (separate, after the worker)

In a fleet mobile client (eventually a `machines` rung added to a copy of the
harness's `index.html`, pointed at the relay):
- Top rung lists machines (from `machines` roster frames).
- Selecting a machine = "connect a HarnessConn for me"; from there the existing
  harness UI works against the tunneled frames unchanged.
- Until that exists, extend `fleet_cli.py` to send harness frames
  (`list`/`new`/`subscribe`/`send`) wrapped in `toMachine`, to test the bridge.

## Build & verify order (suggested)

1. **Worker proxy, one viewer, text-only.** Wrap/unwrap JSON harness frames for a
   single HarnessConn. Test: `fleet_cli` → `@machine {type:"list"}` returns the
   harness's `projects`/`sessions`.
2. **Drive a turn.** Send `{type:"new",pid}` → get `focus`; `{type:"subscribe",cid}`
   → `hello`+history; `{type:"send",text}` → watch `hook` `Stop` with `data.last`
   come back. This is the headline demo: a prompt from "mobile" answered by a real
   remote claude.
3. **PTY bytes** (base64-in-JSON first). Confirm a remote terminal renders.
4. **Per-viewer fan-out.** Two mobiles, two sessions, one machine.
5. **Optimize** PTY to raw binary relay framing if base64 overhead matters.
6. Update `fleet_smoke.py` with a harness-proxy path (spin a real harness on a
   test port, or a stub that speaks its protocol).

## Acceptance check

From a laptop, through `wss://h.atg.link`, pick the `zkllmapi-box` machine, create
a session in a project, send "what is 2+2?", and see the assistant's answer come
back via a `hook` `Stop` frame's `data.last` — with the live transcript streaming
alongside. No changes to clawd-harness.

## Don'ts

- Don't reimplement sessions/PTYs/transcripts in the worker — proxy them.
- Don't edit clawd-harness `server.py`. (If the protocol genuinely lacks
  something, that's a harness PR with a `docs/WS-PROTOCOL.md` update — a separate,
  deliberate decision, not a fleet workaround.)
- Don't collapse the per-viewer connections into one shared harness conn — you'll
  lose independent `cid` subscriptions.
