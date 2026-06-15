# clawd-fleet runbook — operating the live deployment

How to operate, inspect, and recover the running fleet. For *why* it's built this
way, see [`ARCHITECTURE.md`](ARCHITECTURE.md).

## The live deployment (as of handoff)

| Thing | Value |
|---|---|
| Public relay | **`wss://h.atg.link/ws`** (TLS via Let's Encrypt, auto-renew) |
| Mobile UI | **`https://h.atg.link/?t=<TOKEN>`** — relay serves `index.html` at `/` |
| AWS box | `ssh zkllmapi` — Ubuntu 24.04, user `ubuntu`, public IP `174.129.67.164` |
| Relay process | systemd `clawd-fleet-relay` → `python3 relay.py`, bound `127.0.0.1:8788` |
| Relay-node worker | systemd `clawd-fleet-worker` → machine id `zkllmapi-box`, started `--kind relay`. No harness behind it — it registers purely so the **hub shows on the roster** as a muted, non-drivable infra card. Don't expect projects/sessions from it. |
| Laptop proxy worker | launchd `com.clawd.fleet-worker` → machine id `austin-laptop`, bridges to the laptop's harness (`ws://127.0.0.1:8787`) |
| nginx vhost | `/etc/nginx/sites-available/h.atg.link` → proxies `:443` to `127.0.0.1:8788` |
| Code on box | `~/clawd-fleet/` (currently an **scp copy**, not a git clone — see "Drift") |
| Auth | `~/clawd-fleet/fleet.env` (chmod 600): `FLEET_MOBILE_TOKEN` (URL credential) + `FLEET_WORKER_TOKEN` (machine registration) + `FLEET_WORKER_ALLOW=austin-laptop`. `exec` off (no `FLEET_ALLOW_EXEC`). |

> ℹ️ **The box runs a `--kind relay` worker** (`clawd-fleet-worker`, machine id
> `zkllmapi-box`). It has **no harness behind it**, so it holds no projects or
> sessions — it exists only to put the **hub itself** on the roster as a muted,
> non-drivable "relay" card (topology awareness). The UI keys off `kind:"relay"`
> in the roster: it skips it in auto-select and renders it as infra, not a machine
> you open. Real drivable machines (e.g. the laptop) run `--kind machine` (the
> default) pointed at their local harness. *(History: this worker was briefly
> removed 2026-06 as a confusing dead card, then brought back **labeled** instead.)*

> ⚠️ **The box is shared and in production.** It also runs conclave.larv.ai,
> media streaming (mediamtx), and backend.zkllmapi.com. When touching nginx:
> only *add* vhosts, never edit others', and **always `sudo nginx -t` before
> `sudo systemctl reload nginx`**. `relay.larv.ai` is a *different* app (a Fastify
> server on :4000) — not ours; leave it alone.

## Everyday commands

```bash
# status + logs
ssh zkllmapi 'systemctl status clawd-fleet-relay --no-pager'
ssh zkllmapi 'journalctl -u clawd-fleet-relay -n 50 --no-pager'
ssh zkllmapi 'journalctl -u clawd-fleet-relay -f'          # live tail
ssh zkllmapi 'journalctl -u clawd-fleet-worker -f'         # the relay-node worker

# restart / stop / start
ssh zkllmapi 'sudo systemctl restart clawd-fleet-relay clawd-fleet-worker'
ssh zkllmapi 'sudo systemctl stop clawd-fleet-worker'

# is the relay listening + who's connected?
ssh zkllmapi 'ss -tlnp | grep 8788'
ssh zkllmapi 'journalctl -u clawd-fleet-relay | grep -E "online|offline" | tail'

# the tokens (mobile = the URL credential; worker = machine registration)
ssh zkllmapi 'grep -E "MOBILE_TOKEN|WORKER_TOKEN" ~/clawd-fleet/fleet.env'
```
The **mobile token** is what goes in the phone URL: `https://h.atg.link/?t=<MOBILE_TOKEN>`.
To rotate, edit `fleet.env`, `sudo systemctl restart clawd-fleet-relay`, then update
every worker's token (the laptop launchd plist; the box worker reads `fleet.env`).

## Verify the loop

**From a browser (the real path):** open `https://h.atg.link/?t=<TOKEN>` → the
`machines` rung lists online workers → tap `austin-laptop` → its real projects /
sessions load → open a session to drive live Claude (terminal + transcript).

**From the terminal (prototype diagnostics):** the relay is `127.0.0.1`-bound on
the box, so connect via the public `wss://`:
```bash
TOKEN=$(ssh zkllmapi 'cat ~/clawd-fleet/.clawd-fleet.token')
python3 fleet_cli.py --relay wss://h.atg.link --token "$TOKEN"
# at the prompt:  list   ·   @austin-laptop uname -a   ·   @* hostname
```

## Gotchas (these have bitten us)

- **`pkill -f "worker.py"` over SSH kills the SSH shell itself** (the pattern
  matches its own command line) → exit 255. Use the **bracket trick**:
  `pkill -f "[w]orker.py"`. Better: just use `systemctl restart`.
- **Don't run a manual `relay.py` while the service is up** — port 8788 is taken;
  `systemctl stop` first.
- **macOS laptop proxy worker is a launchd agent** —
  `~/Library/LaunchAgents/com.clawd.fleet-worker.plist` (RunAtLoad + KeepAlive,
  machine id `austin-laptop`, `FLEET_RELAY=wss://h.atg.link`; the plist holds the
  relay token so it's chmod 600). Manage it:
  ```bash
  launchctl list | grep fleet-worker            # is it running?
  tail -f ~/Library/Logs/clawd-fleet-worker.log # its log
  launchctl unload ~/Library/LaunchAgents/com.clawd.fleet-worker.plist   # stop
  launchctl load -w ~/Library/LaunchAgents/com.clawd.fleet-worker.plist  # start
  ```
  `HARNESS_TOKEN` isn't in the plist — the worker auto-discovers it from the
  harness's `.clawd-harness.token`. (macOS has no `setsid`; launchd is the right
  persistence mechanism here, mirroring systemd on the Linux box.)
- **Cert renewal** is certbot's systemd timer; nothing to do. To check:
  `ssh zkllmapi 'sudo certbot certificates'`.

## Add a new machine to the fleet

➡️ **Full, self-contained checklist: [`ADD-MACHINE.md`](ADD-MACHINE.md).** Hand
that page to a fresh Claude on the new box — it covers the two pieces this quick
recipe used to omit (the worker's `cryptography` dependency and the shared
passkey file), which are required for the end-to-end channel and are the usual
reason a new machine joins the roster but won't open. The quick version:

First **add the machine id to the relay's allowlist**: edit `fleet.env`'s
`FLEET_WORKER_ALLOW` on the box and `sudo systemctl restart clawd-fleet-relay`
(skip if the allowlist is empty = allow-any). Then, on the new machine (anywhere
with Python 3 + outbound internet):
```bash
python3 -m pip install cryptography      # worker-only dep, needed for the E2E channel
cp <from-existing-machine>/fleet/.clawd-fleet.passkeys.json fleet/    # shared passkey(s)
FLEET_RELAY=wss://h.atg.link FLEET_WORKER_TOKEN=<worker-token> \
  python3 worker.py --machine <unique-id> --host $(hostname) \
  --harness ws://127.0.0.1:8787      # ← omit on a box with no harness (diagnostic only)
```
- Use the **worker** token (not the mobile one); get it from the box's `fleet.env`.
- **`cryptography` + the passkey file are mandatory** to drive real sessions: with
  `FLEET_E2E_REQUIRE=1` (default) the worker refuses to proxy without them. Don't
  copy `.fleet.worker_id.json` — it's per-machine (auto-generated). See
  [`ADD-MACHINE.md`](ADD-MACHINE.md).
- **To drive real Claude sessions**, the machine must run a clawd-harness; point
  `--harness` at it (`HARNESS_TOKEN` auto-discovers from `.clawd-harness.token`).
  Without a harness the worker only answers `ping` (`exec` needs `FLEET_ALLOW_EXEC=1`).
- Linux/systemd: copy `deploy/clawd-fleet-worker.service` (it's the box's
  relay-node unit — change `ExecStart` to `worker.py --machine <id> --harness
  ws://127.0.0.1:8787`, dropping `--kind relay`, which is only for the hub),
  set `EnvironmentFile`/`--machine`, `daemon-reload`, `enable --now`.
- macOS: a launchd agent (see the `com.clawd.fleet-worker.plist` example above).
- It dials out — **no inbound ports / firewall changes needed** on the machine.

## Stand up a relay on a fresh box (reproducible)

1. Copy this repo to the box (ideally `git clone` — see Drift).
2. `cp deploy/fleet.env.example fleet.env`; set a strong `FLEET_TOKEN`
   (`python3 -c "import secrets;print(secrets.token_urlsafe(24))"`).
3. `sudo cp deploy/clawd-fleet-relay.service /etc/systemd/system/ &&
   sudo systemctl daemon-reload && sudo systemctl enable --now clawd-fleet-relay`.
4. Point a domain's A record at the box, then
   `sudo bash deploy/setup_tls.sh <domain> <email>` (adds an nginx vhost + cert).
5. Workers connect with `FLEET_RELAY=wss://<domain>`.

## Deploy updates to the box (scp)

The box's `~/clawd-fleet/` is an **scp copy, not a git checkout** — a historical
artifact, not a constraint. (The repo `clawdbotatg/clawd-harness` is **public**,
so a plain `git clone https://github.com/clawdbotatg/clawd-harness.git` on the box
now works and converting to a real checkout is a viable cleanup — note the box
layout is **flat** vs. the repo's `fleet/` + root split, so a checkout would also
move `_serve_file`'s lookup to the `HERE.parent/<name>` branch.) Until then, ship
changes by copying the files that changed and restarting:
```bash
# from the harness root (fleet code lives in fleet/, the shared UI at the root):
scp fleet/relay.py fleet/worker.py fleet/fleet_ws.py fleet/webauthn.py index.html favicon.png \
    zkllmapi:~/clawd-fleet/
ssh zkllmapi 'sudo systemctl restart clawd-fleet-relay clawd-fleet-worker'
```
The box stays **flat** — `index.html` lands next to `relay.py`, which `_serve_file`
serves via its `HERE/<name>` check.
Then verify with `journalctl -u clawd-fleet-relay -n 20` and the browser/loop
checks above. (A git-clone conversion was deferred in 2026-06 — at the time the
repo was assumed private; it's since **public**, so a clone is now unblocked,
left as a cleanup. Don't `mv` the live dir without a working clone ready: the
running services hold the old inode but a later restart needs the path.)
