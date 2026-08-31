# clawd-fleet (the `fleet/` layer) — orientation for Claude

The **fleet layer of clawd-harness**: drive N machines (each running a harness)
from one phone, through one public relay. It lives in **`clawd-harness/fleet/`**
(folded in from the former standalone `clawd-fleet` repo, now archived).
`README.md` is the user-facing overview; this file orients an agent working
**on** the code.

> **Current state (2026-06) — read this; some details below predate it.**
> - **Monorepo:** this is now `clawd-harness/fleet/`, not a separate repo. The
>   shared `index.html`/`favicon.png` live one level up at the harness root; the
>   relay's `_serve_file` serves the **first existing** of `HERE/<name>` (flat box
>   layout) then `HERE.parent/<name>` (monorepo). All paths below are relative to
>   `fleet/` unless noted.
> - **Auth is defense-in-depth:**
>   - *Relay edge gate* — **passkey-only** (`FLEET_PASSKEY_ONLY=1`, no mobile
>     token); a passkey (WebAuthn) verified in stdlib (`webauthn.py`); the relay
>     withholds the roster until `{type:auth}`. Anti-abuse, **not** the security
>     boundary. The worker token still gates machine registration. There is **no
>     web enrollment** — the passkey pubkey is admin-provisioned into the file.
>   - *End-to-end channel (`fleet-e2e/1`)* — THE boundary. When a mobile opens a
>     machine it runs a passkey-bound authenticated key exchange directly with that
>     machine's **worker** (`e2e.py` + the `[e2e-core]` block in `index.html`): the
>     worker independently verifies a channel-bound passkey (require-UV) over its
>     pinned long-term identity, and **all** harness traffic is AES-GCM end-to-end.
>     The relay routes only ciphertext → a compromised relay is reduced to DoS.
>     Worker session slides 10 min idle / **24 h hard** (`FLEET_E2E_MAX_TTL`
>     default 86400 — the per-machine passkey cadence; silent resume covers
>     everything inside it, and the worker persists resume material to
>     `.fleet.e2e_resume.json` so restarts don't force re-auth. The relay likewise
>     persists edge-session tokens to `.clawd-fleet.sessions.json`). Target: **one
>     passkey per machine per day**, no storms. Spec: **`../docs/fleet/E2E-PROTOCOL.md`**.
>     The relay needs **no** crypto for this (blind passthrough); `cryptography`
>     is a **worker-only** dep. Tests: `test_e2e.py`, `test_e2e_mitm.py`,
>     `test_e2e_interop.py` (Python↔browser byte-for-byte via `node`).
>     **A silent resume is time-boxed** (2026-08-20, `E2E_RESUME_REPLY_MS` 10s in
>     `index.html`): it's one round-trip with no human in it, so a reply that
>     hasn't come back is lost, not slow. Before this the channel sat
>     `handshaking` for the full 90s stale window while every `hsend` "joined
>     in-flight" — a viewer back from a long idle watched the whole fleet resume
>     in the same second, 92s after auth, and a `new` + prompt typed into that
>     gap died on the replaced channel. A resume-phase failure keeps the material,
>     skips the passkey cooldown (nothing was prompted), and the relist chain
>     re-resumes; frames queued on an attempt that never opened are carried into
>     its replacement. The client half of the blank-tty story is
>     `armNewFocusWatch` (root `CLAUDE.md`, `tools/spawnprobe.mjs`).
>   - ***Active machines* — the passkey budget** (2026-08-08). "One per machine
>     per day" MULTIPLIES: N boxes on the roster = **N+1 ceremonies** every
>     morning (the edge gate plus one E2E handshake each), even when you only
>     wanted one box. So a machine can be **switched off** from the machines tab:
>     the page then opens no channel to it, sends it nothing, merges none of its
>     projects/sessions, and never prompts for it. The set is a **deny-list stored
>     on the relay** (`{type:"prefs", inactive:[…]}` in both directions,
>     persisted to `.clawd-fleet.prefs.json`) rather than in localStorage, because
>     unchecking on the phone has to take effect on the desktop too.
>     **Frame ordering is load-bearing:** the relay sends `prefs` *before* a
>     mobile's first `machines` frame (`_send_prefs`) — the page kicks its
>     handshakes off the roster, so a roster that arrived first would unlock the
>     very boxes you switched off. Unknown machine = ON, so a new box needs no
>     config. Switching one off KEEPS its e2e resume material, so switching it
>     back on inside the 24h window is silent. A deep link that NAMES an off
>     machine (a notification tap) switches it back on — explicit navigation beats
>     the checkbox. Tests: `test_relay_prefs.py` (server),
>     `../tools/fleetprobe.mjs` (client).
>   - **The passkey modal is ONE element with TWO owners** (`passkeyOwner` in
>     `index.html`): the edge gate, and each machine's handshake. The relay
>     re-broadcasts the roster every `PING_EVERY` (20s) as a liveness heartbeat,
>     and the `machines` handler used to `hidePasskey()` blindly — which yanked a
>     half-finished per-machine unlock off the screen mid-tap, stranded its
>     channel in `handshaking` forever, and left the page blank until a manual
>     reload. Every `hidePasskey()` call site must now NAME its owner; an
>     unqualified call is a force-hide and should be rare.
> - **Live at `wss://h.atg.link`** (its own subdomain → the box). The relay serves
>   the unified `index.html` there. (The earlier `relay.atg.link` subdomain was
>   retired 2026-06 — DNS + cert still exist on the box but its nginx vhost is
>   disabled; `h.atg.link` is the one production endpoint.)
> - **One UI, two modes.** `index.html` (at the harness root) is **unified and
>   mode-aware** via `window.__FLEET__`: the harness serves it untouched → direct
>   mode; the relay injects the flag (`relay.py` `_serve_file`) → fleet mode
>   (machines rung + passkey). There is **one copy** now (the old cp-to-fleet
>   ritual is gone) — edit the harness root `index.html`, then `git push` + pull on
>   the box (a git checkout now, not scp).
>   **See `../docs/fleet/DEPLOY.md`.** No build, no React (that rebuild was
>   reverted — archived on `archive/react-scaffold-eth`).
> - New files: `webauthn.py`, `e2e.py` (E2E channel crypto), `test_webauthn.py`,
>   `test_relay_passkey.py`, `test_e2e.py`, `test_e2e_mitm.py`, `test_e2e_interop.py`,
>   `test_relay_prefs.py` (the active-machines set), `.fleet.worker_id.json`
>   (worker identity key, gitignored), `.clawd-fleet.prefs.json` (the
>   active-machines set, gitignored).

## The one principle that must not regress
**The `fleet/` layer never modifies or imports the harness.** The harness
(`../server.py`) is a black box reached over its own localhost WebSocket. If you
find yourself wanting to edit `server.py` to make the fleet work, stop — the
right fix almost always lives in the worker (a *client* of the harness) or the
relay. Even though they now share a repo, that boundary is the whole point: keep
`fleet/` import-free of harness internals.

Corollary directions baked into the design:
- **Everyone dials out to the relay.** Workers and mobiles never accept inbound
  connections (machines sit behind NAT). The relay is the only public box.
- **The worker is just another harness client.** It speaks the harness's
  protocol like a browser does, but forwards frames to the relay. This is now
  live: `worker.py` opens one harness connection per remote viewer and pumps
  frames both ways (the prototype `ping`/`exec` handlers remain as diagnostics).
- **The mobile reuses the harness protocol verbatim** below a new top rung.
  Harness stack: `projects → sessions → transcript → tty`. Fleet adds
  `machines` on top; everything below is unchanged. This is now live —
  `index.html` is a fork of the harness's page (see Architecture).

## Architecture
- **relay.py** — public hub. Holds an outbound WS from each worker
  (`machine_id → Conn`) and each mobile (`mobile_id → Conn`); routes by id,
  broadcasts the roster on join/leave, pings to keep NAT mappings warm. Routes
  both JSON control frames **and binary PTY frames** (opcode `0x2`): a worker's
  binary frame is `[1-byte len][mobileId][PTY bytes]` → re-tagged to the mobile
  as `[1-byte len][machineId][PTY bytes]`. On a mobile disconnect it sends every
  worker `{type:"mobileGone", mobile}` so they can drop that viewer's harness
  link. Also **serves the mobile UI**: `GET /` → `index.html`, `GET /favicon.png`
  (so the page and its WS share one origin — `https://h.atg.link/?t=<TOKEN>`).
  Pure stdlib `BaseHTTPRequestHandler` + the framing in `fleet_ws.py`. Runs on
  AWS, bound to `127.0.0.1`, TLS terminated by nginx in front.
- **worker.py** — per-machine agent. Dials the relay, registers a stable
  `machine` id, auto-reconnects with backoff. Two task families share the link,
  disambiguated by field: **`msg.type`** = a harness control frame → proxied
  into a per-viewer **`HarnessLink`** (a client WS to `ws://127.0.0.1:8787`,
  opened lazily on first frame, torn down on `mobileGone`); **`msg.kind`** =
  the prototype `ping`/`exec` diagnostics. Harness→relay: JSON frames wrapped as
  `{type:"reply",to,msg}`, binary PTY as a length-prefixed `0x2` frame. Config:
  `--harness` / `HARNESS_WS` (default `ws://127.0.0.1:8787`) and `--harness-token`
  / `HARNESS_TOKEN` (auto-discovered from `.clawd-harness.token`).
- **fleet_ws.py** — RFC 6455 helpers. `ws_send`/`ws_read_message` (shared, both
  handle text `0x1` + binary `0x2`) and `client_connect` (dials `ws://`/`wss://`
  — clients MUST mask their frames; the relay/server MUST NOT). TLS is
  client-side only; the relay speaks plain ws.
- **../index.html** (the harness root) — the **one** mode-aware UI. In fleet mode
  (`window.__FLEET__`, injected by the relay) the same page runs the relay adapter:
  it dials the relay as a mobile (`?role=mobile`), wraps every outgoing harness
  control frame as `{type:"toMachine",machine,msg}` via `hsend()`, unwraps
  incoming `machineMsg`/binary by machine (`handleRelay`/`handleBinary`), and adds
  a **`machines`** rung above `projects → sessions → transcript → tty` (roster
  cards, `selectMachine`, machine-prefixed hash `#/m/<id>/p/<pid>/s/<cid>`).
  Everything below the rung is the harness UI unchanged. **The relay serves it**
  at `GET /` (so the page + WS share one origin). It's the same file the harness
  serves directly — no fork, no copy; the adapter is gated on `FLEET` and
  localized (search `hsend`, `currentMachine`, `renderMachines`). Known gaps vs.
  direct mode: server-side TTS hits harness-only endpoints (`/tts`, `/config`)
  that the relay doesn't proxy — degrades gracefully (browser TTS still works).
  Image upload IS bridged (relay `do_POST` → worker → local harness `/upload`),
  but note the nginx vhost in front of the relay needs `client_max_body_size`
  raised past its 1MB default or big phone photos 413 at the edge (the UI also
  downscales >900KB images before POSTing, so this mostly can't trigger).
- **fleet_cli.py** — terminal mobile stand-in (prototype `ping`/`exec`); the
  real UI is now `index.html`.
- **fleet_smoke.py** — relay + 2 workers + scripted mobile; asserts the
  prototype `ping`/`exec`/fan-out loop. Run after touching routing.
- **fleet_proxy_smoke.py** — the harness-proxy loop: relay + worker + an embedded
  **mock harness** (speaks WS-PROTOCOL.md, no real `claude` needed). Asserts
  `list`/`subscribe`/`send`→`Stop` and a **binary PTY frame tunneled back**. Run
  after touching the worker proxy or relay binary routing.

## Run / test
- Local loop: `python3 relay.py` + `python3 worker.py --machine X` + `python3 fleet_cli.py`.
- `python3 fleet_smoke.py` — prototype loop assertion (exits non-zero on failure).
- `python3 fleet_proxy_smoke.py` — harness-proxy loop assertion (mock harness).
- `python3 test_relay_prefs.py` — the shared active-machines set: `prefs` before
  the first roster, echoed to every device, persisted, sanitized. Run after
  touching prefs or the mobile auth path. Client half: `cd ../tools && node fleetprobe.mjs`.
- Env: `FLEET_MOBILE_TOKEN` + `FLEET_WORKER_TOKEN` (auth; both fall back to
  `FLEET_TOKEN`, then `.clawd-fleet.token`), `FLEET_WORKER_ALLOW` (csv machine
  allowlist), `FLEET_ALLOW_EXEC=1` (enable the `exec` diagnostic, off by default),
  `FLEET_RELAY` (worker→relay url), `FLEET_PORT`, `FLEET_BIND`, `FLEET_MACHINE`,
  plus `HARNESS_WS` / `HARNESS_TOKEN` (worker → local harness).

## Live deployment (the AWS box)
- Host: `ssh zkllmapi` (Ubuntu, public IP 174.129.67.164). **Shared, in
  production** — also runs conclave.larv.ai, media streaming (mediamtx),
  backend.zkllmapi.com. Touch nginx carefully; only add vhosts, never edit
  others'. Always `nginx -t` before reload.
- Relay: `wss://h.atg.link` → nginx → `127.0.0.1:8788`. Cert via certbot
  (auto-renew). Code at `~/clawd-harness` on the box — a **git checkout** of
  `clawdbotatg/clawd-harness` (deploy = `git pull`, not scp). Box mirrors the repo
  layout: `relay.py`/`worker.py` in `fleet/`, controller package + `index.html` above.
- Services: `clawd-fleet-relay` and `clawd-fleet-worker` (systemd, enabled,
  auto-restart). `journalctl -u clawd-fleet-relay -f` to watch. Units +
  `setup_tls.sh` are versioned in `deploy/`.
- The box's `clawd-fleet-worker` runs **`--kind relay`** (`--machine
  clawd-nerve-cord`, no harness behind it). It registers purely so the **hub shows on
  the roster** as a muted, non-drivable "relay" card (topology awareness) — it
  holds no projects/sessions. The UI keys off `kind:"relay"` in the roster
  (`renderMachines`): skipped in auto-select, rendered as infra not a machine you
  open. *(It was briefly removed 2026-06 as a confusing dead card, then brought
  back labeled.)* The real **harness-proxy worker** runs on a machine that has a
  harness (i.e. `--kind machine`, the default — e.g.
  the laptop: `FLEET_RELAY=wss://h.atg.link FLEET_TOKEN=… python3 worker.py
  --machine <id> --harness ws://127.0.0.1:8787`). On the laptop it's now
  **daemonized via launchd** (`./daemon-worker.sh install --host atg`, label
  `com.clawd.fleet-worker`, RunAtLoad + KeepAlive — the worker companion to the
  harness's `daemon.sh`) so phone access is always-on across reboots/crashes.
  Config + the worker token come from a gitignored **`fleet.env`** that
  `worker.py` self-loads (`_load_env_file`), so the secret stays out of the plist
  — same pattern as the harness's `.clawd-harness.env`.
- **Updating prod:** the box is a **git checkout** at `~/clawd-harness` (mirrors the
  repo layout — `relay.py`/`worker.py` in `fleet/`, `index.html`/`favicon.png` at the
  repo root one level up; the relay's `_serve_file` checks `HERE/<name>` then
  `HERE.parent/<name>`, which covers it). To ship a change, push and pull:
  `git push origin main` then
  `ssh zkllmapi 'cd ~/clawd-harness && git pull && sudo systemctl restart clawd-fleet-relay clawd-fleet-worker clawd-controller'`
  (UI-only `index.html` edits skip the restart — served fresh per request).
  Don't `mv` the live dir for a clone unless the clone is ready — services hold the
  old inode but a later restart needs the path. See `../docs/fleet/RUNBOOK.md`.
- **gotcha:** `pkill -f "worker.py"` over SSH matches its own command line and
  kills the shell (exit 255). Use the bracket trick: `pkill -f "[w]orker.py"`.

## Fleet skills library (2026-08-30)
The relay box also holds the **private skill library** (`.clawd-fleet.skills/`,
gitignored; worker-token HTTP `/skills/manifest|get|lib|put`): user-written
skill files, one list on every device. The UI's 📚 picker fetches it
(`skillsLib` over the mobile socket; direct-mode harness proxies) and a tap
**pastes the SKILL.md body** into the open session; ✕ → `skillsRm` → the
store dir moves to `.trash/` (recoverable). Publish with `share/bin/skillput`.
**Deliberately decoupled from machines** — nothing installs into
`~/.claude/skills/`; the same-day sync that did was removed and
`_skills_sync_cleanup` (one-shot in worker.py) undoes its installs. **Deep
doc: `../docs/fleet/SKILLS.md`**; test: `test_skills_lib.py`.

## Deep docs
- **`../docs/fleet/ADD-MACHINE.md`** — self-contained checklist to add a new
  machine to the fleet (the doc to hand a fresh Claude on the new box). Covers the
  E2E prerequisites — the `cryptography` worker dep + the shared passkey file —
  that the older RUNBOOK snippet omitted.
- **`../docs/fleet/ARCHITECTURE.md`** — design, rationale, decision log, traps.
- **`../docs/fleet/HARNESS-PROXY.md`** — the proxy-worker design (✅ shipped).
- **`../docs/fleet/RUNBOOK.md`** — operating the live box.
- **`../docs/WS-PROTOCOL.md`** — the harness contract to bridge.

## Roadmap (the reason this layer exists)
1. ~~**Harness-proxy worker**~~ — ✅ done. Worker connects to the local harness as
   a client, one connection per remote viewer, pumps JSON metadata + binary PTY
   to/from the relay. Verified live through `wss://h.atg.link` against a real
   laptop harness (roster, `list`, `new`, `subscribe`, 256KB PTY snapshot, live
   hooks all tunneled).
2. ~~**Relay tunnels binary frames**~~ — ✅ done. Length-prefixed opcode `0x2`
   routing, per machine + per client (see relay.py).
3. ~~**Mobile UI**~~ — ✅ done. `index.html` (a fork of the harness page + a relay
   adapter) adds a `machines` rung above the unchanged harness stack; the relay
   serves it. Verified live on `https://h.atg.link`: roster, drilling into a real
   machine's projects/sessions, live terminal (binary PTY) + transcript + hooks,
   machine-prefixed hash routing — all over TLS. Image upload / server TTS are the
   known gaps (harness-only endpoints, not proxied).
4. ~~**Auth hardening**~~ — ✅ done. Split secrets: `FLEET_MOBILE_TOKEN` (the
   user's URL credential) vs `FLEET_WORKER_TOKEN` (authorizes a machine to
   register) — both fall back to `FLEET_TOKEN` for single-token setups. Worker
   **allowlist** (`FLEET_WORKER_ALLOW`, csv of machine ids). The `exec` shell
   handler is **off by default** (gated behind `FLEET_ALLOW_EXEC=1` on the
   worker) — the product path is the harness proxy. Relay never logs tokens.
   Verified live: old token 403'd, mobile token can't register a worker,
   non-allowlisted worker rejected, `exec` disabled. The mobile token no longer
   lingers in the URL — `index.html` migrates `?t=` into localStorage on load and
   strips it (replaceState); no-token shows a paste-the-token screen; the QR
   re-appends it for pairing. (The box was migrated off scp to a `git clone` at
   `~/clawd-harness` — deploy is now `git pull` + restart.)

## Conventions
- Git identity here (under `~/clawd/`): **clawdbotatg** /
  `clawd@buidlguidl.com`, over **HTTPS**. Remote: `clawdbotatg/clawd-harness`
  (the fleet lives in this monorepo now; the standalone `clawd-fleet` repo is archived).
- **Never commit** `.clawd-fleet.token`, `fleet.env`, `.clawd-fleet.machine`,
  `*.log` (gitignored). Scan diffs for secrets before committing.
- Pure Python stdlib, no deps — keep it that way (matches clawd-harness).
