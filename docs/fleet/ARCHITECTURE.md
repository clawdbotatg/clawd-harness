# clawd-fleet architecture & decision record

Everything learned building this, and *why* it's shaped the way it is. Read this
before changing the transport or the layering. Companion docs:
[`RUNBOOK.md`](RUNBOOK.md) (operating the live box) and
[`HARNESS-PROXY.md`](HARNESS-PROXY.md) (the next implementation step).

## The problem

One phone should drive many machines, each running
[clawd-harness](https://github.com/clawdbotatg/clawd-harness) (= "one machine's
Claude Code environment"). Send a message from mobile → a hub → the right
machine → and stream the result back.

## The core constraint that determines everything: who dials whom

Worker machines are laptops / home boxes **behind NAT and firewalls**. You
cannot open an inbound connection to them. So:

> **Everyone dials *out* to the relay. The relay never connects back into anyone.**

The relay is the single public box (it has a routable address + TLS). Workers
hold a persistent outbound WebSocket to it; mobiles do the same. The relay is a
pure **router** between them. This is the same reason ngrok, Tailscale, and CI
runners all work the way they do — it's not a stylistic choice, it's the only
topology that works through NAT.

```
 📱 mobile ──wss──▶  relay (public: wss://h.atg.link)  ◀──wss── worker (machine A)
                          │  routes frames, tagged per machine ◀──wss── worker (machine B)
                          ▼
                    (never dials back out)
```

## The layering insight (this is the whole design)

clawd-fleet is an **abstraction layer on top of clawd-harness**, not a fork of
it. The hard parts — running `claude` in PTYs, sessions, projects, transcripts,
the working/idle pill — already exist in the harness and must not be
reimplemented. Two consequences:

1. **The worker is just another harness client.** The harness already serves a
   full WebSocket protocol (see harness `docs/WS-PROTOCOL.md`). A browser is one
   client. The fleet worker is another — it speaks the *same* protocol but
   forwards frames to the relay instead of painting a screen. → **zero harness
   changes.**

2. **The mobile reuses the harness protocol verbatim.** The harness UI is
   already a swipe stack: `projects → sessions → transcript → tty`. The fleet
   adds exactly **one rung on top — `machines`** — and everything below it
   already works, because the relay just tunnels harness frames tagged by
   machine.

So the mental model is strictly nested:

```
clawd-fleet  (machines: discover, route, fan-out)
  └── clawd-harness  (projects → sessions → transcript → tty)   ← unchanged, fleet-unaware
```

**The invariant to protect:** clawd-fleet never imports or edits clawd-harness.
If a change seems to require editing the harness, it almost certainly belongs in
the worker (a client) or the relay instead. That boundary is the value.

## Components

- **relay.py** — the public hub. Keeps `machine_id → Conn` (workers) and
  `mobile_id → Conn` (mobiles); routes by id; broadcasts the roster on
  join/leave; pings every 20s to keep NAT mappings warm. Pure stdlib
  (`BaseHTTPRequestHandler` + the WS framing in `fleet_ws.py`). Binds
  `127.0.0.1`; nginx terminates TLS in front of it.
- **worker.py** — per-machine agent. Dials the relay, registers a stable
  `machine` id, reconnects with exponential backoff. **Now a harness proxy**
  (✅ [`HARNESS-PROXY.md`](HARNESS-PROXY.md)): a harness control frame (`msg.type`)
  is forwarded into a per-viewer `HarnessLink` (a client WS to the local harness),
  pumping JSON + binary PTY both ways. The prototype `ping`/`exec` (`msg.kind`)
  handlers remain as diagnostics.
- **fleet_ws.py** — RFC 6455 helpers. `ws_send`/`ws_read_message` (shared
  server+client framing, text + binary) and `client_connect` (dials `ws://` *or*
  `wss://`). **Clients MUST mask their frames; servers MUST NOT** —
  `ws_send(..., mask=True)` on the worker/mobile side, `mask=False` on the relay
  side. TLS is client-side only (the relay speaks plain ws behind nginx).
- **index.html** — the mobile UI: a fork of the harness's page + a thin relay
  adapter (wrap outgoing frames as `toMachine`, unwrap `machineMsg`/binary by
  machine) plus a `machines` rung above the unchanged harness stack. Served by the
  relay at `GET /`.
- **fleet_cli.py** — a terminal stand-in for the mobile (prototype `ping`/`exec`);
  the real UI is now `index.html`.
- **fleet_smoke.py** — end-to-end test of the prototype loop: relay + 2 workers +
  scripted mobile; asserts roster/ping/exec/fan-out/error.
- **fleet_proxy_smoke.py** — end-to-end test of the proxy loop with an embedded
  mock harness (no real `claude`): `list`/`subscribe`/`send`→`Stop` + a tunneled
  binary PTY frame. Run both after any routing change.

## Protocol (current)

JSON text frames carry control + metadata; the relay routes by id and treats the
inner `msg` as opaque. **Binary frames (opcode `0x2`) carry raw PTY bytes**,
length-prefixed with a routing id (shipped in the proxy step).

- mobile→relay: `{type:"list"}` · `{type:"toMachine", machine:<id|"*">, msg:{…}}`
- relay→mobile: `{type:"machines", machines:[{id,host,kind,online,lastSeen,stats}]}`
  (`kind`: `"machine"` = drivable harness host · `"relay"` = the hub box itself,
  shown as a muted, non-drivable infra card) ·
  `{type:"machineMsg", machine:<id>, msg:{…}}` · `{type:"error", error}` ·
  binary `[len][machineId][PTY…]`
- relay→worker: `{type:"task", from:<mobileId>, msg:{…}}` ·
  `{type:"mobileGone", mobile:<id>}`
- worker→relay: `{type:"reply", to:<mobileId>, msg:{…}}` · `{type:"status", msg:{…}}` ·
  binary `[len][mobileId][PTY…]`

Addressing: each mobile gets a relay-assigned `mobile_id`; the worker echoes it
back in `reply.to` so the relay routes the answer to the right phone. `machine:"*"`
fans a task out to every worker.

Prototype `msg` kinds: `{kind:"ping"}`→`{kind:"pong",host,machine,ts}`;
`{kind:"exec",cmd}`→ streamed `{kind:"output",stream,data}` … `{kind:"exit",code}`.

## What's proven vs. what's next

✅ **Full vertical slice, in production.** Relay live on AWS behind TLS
(`wss://h.atg.link`) as a systemd service. The **harness-proxy worker** and the
**mobile UI** are shipped: from a browser at `https://h.atg.link/?t=<TOKEN>` you
pick a machine, drill into its real projects/sessions, and drive a live Claude
session — terminal (binary PTY), transcript, and working pill all tunneled. The
laptop proxy worker is a launchd agent (`com.clawd.fleet-worker`). Both
`fleet_smoke.py` and `fleet_proxy_smoke.py` pass.

✅ **Image upload** bridges through the relay (phone → worker → harness
`/upload`). ✅ **Auth hardened** — split mobile/worker tokens, worker allowlist,
`exec` off by default (see decision log).

✅ **Token kept out of the URL** — `index.html` migrates `?t=` into localStorage
and strips it from the bar (replaceState); tokenless → a paste-the-token screen;
the QR re-appends it for pairing.

🚧 **Remaining:** server-side TTS isn't proxied (`/tts`/`/config` are
harness-only); the box is still scp-deployed (a leftover — the repo is now
**public**, so converting the box to a `git clone` is unblocked).

## Decision log (and the traps we hit)

- **Pure Python stdlib, no deps** — matches clawd-harness; keep it that way. The
  WS framing is hand-rolled in `fleet_ws.py` (same code style as the harness's
  `server.py`).
- **Token in the query string (`?t=`)** — parity with the harness. **Split into
  two secrets** (`FLEET_MOBILE_TOKEN` vs `FLEET_WORKER_TOKEN`) so a leaked mobile
  URL can't register a worker; plus a worker allowlist (`FLEET_WORKER_ALLOW`) and
  `exec` off by default (`FLEET_ALLOW_EXEC`). Both tokens fall back to
  `FLEET_TOKEN` for single-token dev. The mobile token no longer lingers in the
  URL — the page migrates `?t=` into localStorage and strips it on load.
- **Relay binds 127.0.0.1 + nginx TLS** — chosen over opening a raw port in the
  AWS security group, because (a) no SG change needed (80/443 already open for
  the box's other services) and (b) real `wss://` instead of plaintext `ws://`.
- **TLS is client-side only** — the relay stays plain `ws` behind nginx; only
  `fleet_ws.client_connect` does `ssl`. Simpler relay, standard ops.
- **Reconnect is the worker's job** — exponential backoff (1→30s). The relay
  supersedes a stale connection when a worker with the same id reconnects.
- **`.gitignore` inline-comment trap** — in `.gitignore`, `#` only starts a
  comment at the *start* of a line; an inline `# comment` becomes part of the
  pattern and silently breaks the ignore. Put comments on their own lines.
- **`pkill -f "worker.py"` over SSH kills your own shell** — the pattern matches
  the ssh command line itself (which contains "worker.py"), exit 255. Use the
  bracket trick: `pkill -f "[w]orker.py"`.
- **`gh` account switch** — `gh` has both `austintgriffith` (active) and
  `clawdbotatg` logged in; creating repos under clawdbotatg needs
  `gh auth switch --user clawdbotatg` first, then switch back.
- **macOS has no `setsid`** — for laptop background workers use `nohup … & disown`
  (the systemd units handle persistence on the Linux box).
