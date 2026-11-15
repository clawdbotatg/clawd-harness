# clawd-fleet runbook — operating the live deployment

How to operate, inspect, and recover the running fleet. For *why* it's built this
way, see [`ARCHITECTURE.md`](ARCHITECTURE.md).

## The live deployment (as of handoff)

| Thing | Value |
|---|---|
| Public relay | **`wss://h.atg.link/ws`** (TLS via Let's Encrypt, auto-renew) |
| Mobile UI | **`https://h.atg.link`** — relay serves `index.html` at `/`. Passkey sign-in; add `?t=<MOBILE_TOKEN>` only if the relay runs without `FLEET_PASSKEY_ONLY` ([`DEPLOY.md`](DEPLOY.md) → Auth) |
| AWS box | **`clawd-nerve-cord`** (the lobster's central spine — the one always-on box that relays signals to the fleet *and* hosts the AI/PM brain). Reached via `ssh zkllmapi` (the ssh alias + the `zkllmapi.com` domain are unchanged). Ubuntu 24.04, user `ubuntu`, public IP `174.129.67.164` |
| Relay process | systemd `clawd-fleet-relay` → `python3 relay.py`, bound `127.0.0.1:8788` |
| Relay-node worker | systemd `clawd-fleet-worker` → machine id `clawd-nerve-cord`, started `--kind relay`. No harness behind it — it registers purely so the **hub shows on the roster** as a muted, non-drivable infra card. Don't expect projects/sessions from it. |
| Laptop proxy worker | launchd `com.clawd.fleet-worker` → machine id `austin-laptop`, bridges to the laptop's harness (`ws://127.0.0.1:8787`) |
| nginx vhost | `/etc/nginx/sites-available/h.atg.link` → proxies `:443` to `127.0.0.1:8788` |
| Code on box | `~/clawd-harness/` — a **git checkout** of `clawdbotatg/clawd-harness` (deploy = `git pull`; mirrors repo layout: `fleet/` + controller package + root `index.html`) |
| Auth | `~/clawd-harness/fleet/fleet.env` (chmod 600): `FLEET_WORKER_TOKEN` (machine registration) + `FLEET_WORKER_ALLOW` (machine ids) + `FLEET_PASSKEY_ONLY=1` (or a `FLEET_MOBILE_TOKEN`, the URL credential, when it is unset). `exec` off (no `FLEET_ALLOW_EXEC`). |

> ℹ️ **The box runs a `--kind relay` worker** (`clawd-fleet-worker`, machine id
> `clawd-nerve-cord`). It has **no harness behind it**, so it holds no projects or
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

# which auth mode + the worker token
ssh zkllmapi 'journalctl -u clawd-fleet-relay | grep "auth:" | tail -1'
ssh zkllmapi 'grep -E "PASSKEY_ONLY|WORKER_TOKEN" ~/clawd-harness/fleet/fleet.env'
```
To rotate the worker token, edit `fleet.env`, `sudo systemctl restart clawd-fleet-relay`,
then update every worker's `fleet/fleet.env`.

## Verify the loop

Open `https://h.atg.link`, pass the passkey → the `machines` rung lists online
workers → tap one → its real projects / sessions load → open a session to drive
live Claude (terminal + transcript). There is no terminal check against
production: `fleet_cli.py` cannot do a passkey, so it only works against a
local relay started with `FLEET_REQUIRE_PASSKEY=0`.

## Gotchas (these have bitten us)

- **`pkill -f "worker.py"` over SSH kills the SSH shell itself** (the pattern
  matches its own command line) → exit 255. Use the **bracket trick**:
  `pkill -f "[w]orker.py"`. Better: just use `systemctl restart`.
- **Don't run a manual `relay.py` while the service is up** — port 8788 is taken;
  `systemctl stop` first.
- **macOS laptop proxy worker is a launchd agent** —
  `~/Library/LaunchAgents/com.clawd.fleet-worker.plist` (RunAtLoad + KeepAlive,
  installed by `fleet/daemon-worker.sh install`; relay URL, worker token and
  machine id come from the gitignored `fleet/fleet.env`, which the worker loads
  itself). Manage it with `fleet/daemon-worker.sh status | logs | restart | uninstall`.
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
- Use the **worker** token; get it from the box's `fleet.env`.
- **`cryptography` + the passkey file are mandatory** to drive real sessions: with
  `FLEET_E2E_REQUIRE=1` (default) the worker refuses to proxy without them. Don't
  copy `.fleet.worker_id.json` — it's per-machine (auto-generated). See
  [`ADD-MACHINE.md`](ADD-MACHINE.md).
- **To drive real Claude sessions**, the machine must run a clawd-harness; point
  `--harness` at it (`HARNESS_TOKEN` auto-discovers from `.clawd-harness.token`).
  Without a harness the worker only answers `ping` (`exec` needs `FLEET_ALLOW_EXEC=1`).
- Linux/systemd: `deploy/clawd-fleet-worker.service` is the box's relay-node
  unit — use it as a template only (see [`ADD-MACHINE.md`](ADD-MACHINE.md) step 6).
- macOS: `fleet/daemon-worker.sh install`.
- It dials out — **no inbound ports / firewall changes needed** on the machine.

## Stand up a relay on a fresh box (reproducible)

1. `git clone https://github.com/clawdbotatg/clawd-harness.git` on the box (it's
   public — see "Deploy updates to the box").
2. `cp deploy/fleet.env.example fleet.env`; set a strong `FLEET_WORKER_TOKEN`
   (`python3 -c "import secrets;print(secrets.token_urlsafe(24))"`) and pick the
   auth mode (`FLEET_PASSKEY_ONLY=1`, or a `FLEET_MOBILE_TOKEN`).
3. The units assume the checkout is at `/home/ubuntu/clawd-harness`.
   `sudo cp deploy/clawd-fleet-relay.service /etc/systemd/system/ &&
   sudo systemctl daemon-reload && sudo systemctl enable --now clawd-fleet-relay`.
4. Point a domain's A record at the box, then
   `sudo bash deploy/setup_tls.sh <domain> <email>` (adds an nginx vhost + cert).
5. Workers connect with `FLEET_RELAY=wss://<domain>`.

## Deploy updates to the box (git pull)

The box's `~/clawd-harness/` is a **git checkout** of `clawdbotatg/clawd-harness`
(public → clones/pulls over HTTPS, no creds). It mirrors the repo layout:
`relay.py`/`worker.py` under `fleet/`, the controller package at the root, and the
shared `index.html`/`favicon.png` at the root (one level above `relay.py`, so
`_serve_file` finds them via its `HERE.parent/<name>` branch). Ship changes by
pushing and pulling:
```bash
# from the harness root, after committing:
git push origin main
ssh zkllmapi 'cd ~/clawd-harness && git pull && \
    sudo systemctl restart clawd-fleet-relay clawd-fleet-worker clawd-controller'
```
UI-only `index.html` edits need **no restart** — the relay serves it fresh per
request, so just `git pull` on the box. Gitignored runtime/secret files live next
to the code that reads them (not in git): `fleet.env` + `.clawd-fleet.{token,passkeys.json,machine}`
in `fleet/`; `.env.controller` + `.clawd-controller.tasks.jsonl` at the repo root.
Then verify with `journalctl -u clawd-fleet-relay -n 20` and the browser check
above. Normally none of this is needed by hand: the box pulls itself
([`DEPLOY.md`](DEPLOY.md) → "Self-update on the box").
