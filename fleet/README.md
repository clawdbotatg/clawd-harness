# clawd-fleet

**Drive a fleet of machines — each running [clawd-harness](https://github.com/clawdbotatg/clawd-harness) — from one phone, through one public relay.**

clawd-fleet is an **abstraction layer on top of clawd-harness**. A single
clawd-harness is "one machine's Claude environment" (projects, sessions, PTYs,
transcripts). clawd-fleet orchestrates **N** of them: every machine dials out to
one public **relay**, and a mobile client reaches any machine — or all at
once — through it.

```
 📱 mobile ──wss──▶  relay  (public box, e.g. wss://h.atg.link)
                       │   token + passkey gate, then routes frames per machine
        ┌──────────────┼───────────────┐
        ▼              ▼                ▼
     worker         worker           worker          ← clawd-fleet
        │              │                │
   ws://127.0.0.1  ws://127.0.0.1   ws://127.0.0.1   ← localhost
        │              │                │
   clawd-harness  clawd-harness    clawd-harness     ← unchanged, fleet-unaware
   (machine A)    (machine B)      (machine C)
```

## The design principle: the harness stays untouched

clawd-fleet never reaches *inside* clawd-harness. The relay is the one public
box; workers and mobiles only ever **dial out** to it (so machines behind NAT
just work). And the per-machine **worker is just another client of the harness's
own WebSocket** — it speaks the same protocol a browser does, but forwards frames
to the relay instead of rendering them.

Consequence: the mobile UI reuses the harness's protocol verbatim. The harness's
swipe stack is already `projects → sessions → transcript → tty`; the fleet adds
**one rung on top — `machines`** — and everything below it already works.

- **clawd-harness** = one self-contained unit. Knows nothing about the fleet.
- **clawd-fleet** = orchestrates harnesses. Wraps them; never modifies them.

## Docs (start here if you're taking over)

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — the design, the *why*, the
  decision log, and every trap we hit. Read before changing the transport.
- **[docs/HARNESS-PROXY.md](docs/HARNESS-PROXY.md)** — the proxy-worker design
  (✅ shipped): how the worker bridges the local harness to the relay so the phone
  drives real Claude sessions.
- **[docs/RUNBOOK.md](docs/RUNBOOK.md)** — operate the live box: services, logs,
  recovery, adding machines, standing up a new relay.
- **clawd-harness `docs/WS-PROTOCOL.md`** — the harness's WebSocket contract the
  proxy worker bridges to. (`../clawd-harness/docs/WS-PROTOCOL.md`.)

## Status

✅ **Transport proven & deployed.** Relay live on AWS behind TLS
(`wss://h.atg.link`), as a systemd service; verified one mobile driving multiple
machines with fan-out, over the public relay.

✅ **Harness-proxy worker live.** The worker now connects to its local
clawd-harness as a client and bridges frames to the relay — one harness
connection per remote viewer, JSON metadata **and** binary PTY bytes (opcode
`0x2`, length-prefixed and routed per machine + per client). Verified live
through `wss://h.atg.link` against a real laptop harness: machine roster,
project/session discovery, session creation, subscribe + a 256KB PTY snapshot,
and live turn-signal hooks all tunneled end-to-end. The harness is unmodified.

✅ **Mobile UI live.** `index.html` is a fork of the harness's own page with a
thin relay adapter: it dials the relay as a mobile, wraps/unwraps harness frames
per machine, and adds a **`machines`** rung above the unchanged
`projects → sessions → transcript → tty` stack. The relay serves it, so the whole
thing is one URL: **`https://h.atg.link/?t=<TOKEN>`**. Verified end-to-end on
production from a browser — roster, drilling into a real machine's
projects/sessions, the live terminal + transcript + working pill, all over TLS.

✅ **Image upload works** end-to-end (phone → relay → worker → the target
machine's harness `/upload`; the image lands where that session's Claude can Read
it).

✅ **Auth hardened.** Split secrets — a **mobile** token (the URL credential) vs a
**worker** token (authorizes a machine to register), so a leaked URL can't
impersonate a worker. A worker **allowlist** restricts which machine ids may join.
The `exec` shell handler is **off by default** (the product path is the harness
proxy). Verified live: old token rejected, mobile token can't register a worker,
non-allowlisted worker rejected.

✅ **Token kept out of the address bar.** `?t=` is migrated into localStorage on
first load and stripped from the URL; a tokenless device gets a paste-the-token
screen; the QR re-appends it for pairing.

✅ **Passkey second factor (WebAuthn).** Beyond the mobile token (factor 1), the
relay gates the roster behind a hardware-backed **passkey** assertion (Touch ID /
Face ID / a security key) — verified server-side in pure stdlib (`webauthn.py`:
P-256 ECDSA + COSE/CBOR + all spec checks). A successful assertion mints a 24h
session so reconnects don't re-prompt. Enrollment is gated by the token **and**
`FLEET_ALLOW_ENROLL` (off by default), so a leaked token alone can't register a
rogue credential. The private key never leaves the device's secure enclave; we
only verify. (This replaced a short-lived wallet-signature experiment + a
scaffold-eth/Vercel rebuild — both archived on `archive/react-scaffold-eth`; the
thin, build-free, relay-served single-file UI is back.)

🚧 **Remaining:** server-side TTS isn't proxied; the box is scp-deployed (a
leftover — the repo is now **public**, so a `git clone` on the box is unblocked);
the mobile UI is still a hand-synced fork of the harness `index.html` (unifying
the two is the next step).

## Files

| File | Role |
|---|---|
| `relay.py` | the public hub. Routes `mobile ⇄ machine` by id (JSON + binary PTY) and serves the mobile UI. Runs on the AWS box. |
| `index.html` | the mobile UI — a fork of the harness page + a relay adapter, adding a `machines` rung. Served by the relay at `/`. |
| `worker.py` | the per-machine agent. Dials out, registers, runs tasks, reconnects. |
| `fleet_ws.py` | stdlib WebSocket helpers — server framing + a `ws://`/`wss://` client dialer. |
| `webauthn.py` | pure-stdlib passkey verifier (P-256 ECDSA + CBOR/COSE + WebAuthn checks). |
| `fleet_cli.py` | terminal stand-in for the mobile (prototype diagnostics). |
| `fleet_smoke.py` · `fleet_proxy_smoke.py` | end-to-end routing + harness-proxy tests (mock harness, no real `claude`). |
| `test_webauthn.py` · `test_relay_passkey.py` | passkey verifier + relay-gate tests (no browser). |
| `deploy/` | `setup_tls.sh`, systemd units, `fleet.env.example` — the box setup as code. |

## Try it locally (three terminals)

```bash
export FLEET_TOKEN=dev FLEET_REQUIRE_PASSKEY=0   # passkey off for the CLI loop
python3 relay.py                          # the hub (0.0.0.0:8788)
python3 worker.py --machine my-laptop     # a machine
python3 fleet_cli.py                       # the "mobile"
```
At the `>` prompt: `list` · `@my-laptop uname -a` · `@* hostname` · `ping *`

One-shot check: `python3 fleet_smoke.py` (spins up everything, asserts the loop).

## Deploy the relay (the public box)

1. Copy the repo to the box; `cp deploy/fleet.env.example fleet.env` and set a
   strong `FLEET_TOKEN` (`python3 -c "import secrets;print(secrets.token_urlsafe(24))"`).
2. Install the services: `sudo cp deploy/*.service /etc/systemd/system/ &&
   sudo systemctl daemon-reload && sudo systemctl enable --now clawd-fleet-relay`.
3. Point a domain's A record at the box, then `sudo bash deploy/setup_tls.sh <domain>`
   — nginx vhost → relay + a Let's Encrypt cert. Relay is now at `wss://<domain>/ws`.

The relay binds `127.0.0.1`; nginx terminates TLS in front of it. Workers off-box
connect with `FLEET_RELAY=wss://<domain>`.

## Protocol

JSON text frames carry control + metadata; **binary frames (opcode `0x2`) carry
raw PTY bytes**, length-prefixed with a routing id so no JSON envelope is needed.

- mobile→relay: `{type:"list"}` · `{type:"toMachine", machine:<id|"*">, msg:{…}}`
- relay→mobile: `{type:"machines", machines:[…]}` · `{type:"machineMsg", machine:<id>, msg:{…}}` · `{type:"error"}` · binary `[len][machineId][PTY…]`
- relay→worker: `{type:"task", from:<mobileId>, msg:{…}}` · `{type:"mobileGone", mobile:<id>}`
- worker→relay: `{type:"reply", to:<mobileId>, msg:{…}}` · `{type:"status", msg:{…}}` · binary `[len][mobileId][PTY…]`

The `msg` envelope is opaque to the relay (it only routes). Two families:
**harness-proxy** (`msg.type` = a clawd-harness control frame like
`subscribe`/`new`/`send`/`input`, forwarded verbatim — see
[`../clawd-harness/docs/WS-PROTOCOL.md`](https://github.com/clawdbotatg/clawd-harness/blob/main/docs/WS-PROTOCOL.md))
and **prototype diagnostics** (`msg.kind`: `ping`/`pong`, `exec`→ streamed
`output` … `exit`).

## Security

- **Passkey second factor.** The token gets you to the door; a hardware-backed
  **passkey** (WebAuthn) gets you in. The relay issues a challenge, the device
  signs it with Touch/Face ID, and the relay verifies the assertion against the
  enrolled credential (`webauthn.py`, pure stdlib). `FLEET_RP_ID` / `FLEET_ORIGIN`
  pin the domain; `FLEET_REQUIRE_PASSKEY` (on by default) enforces it;
  `FLEET_ALLOW_ENROLL` (off by default) gates registering new credentials.
- **Two separate tokens.** `FLEET_MOBILE_TOKEN` is the user's access credential
  (it rides in the page URL); `FLEET_WORKER_TOKEN` authorizes a machine to
  register. Keeping them separate means a leaked mobile URL **cannot** register a
  rogue worker. (Both fall back to `FLEET_TOKEN` for single-token dev setups.)
- **Worker allowlist** (`FLEET_WORKER_ALLOW`) — only listed machine ids may join.
- **`exec` is off by default** (gated behind `FLEET_ALLOW_EXEC=1`). The product
  path is the harness proxy; raw remote shell isn't enabled unless you opt in.
- **TLS always** — the relay binds `127.0.0.1`; nginx terminates `wss://`.
- **Token not left in the URL.** `?t=` is migrated into localStorage and stripped
  from the address bar on load, so it can't leak via history/screenshots. The QR
  re-appends it only when you ask to pair a device.
- Still true: driving a session is full bypass-permissions Claude on that machine
  (that's the product), so the mobile token is powerful — guard it.
