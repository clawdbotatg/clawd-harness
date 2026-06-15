# Deploy & operate clawd-fleet

No Vercel, no build step. The whole system is pure-Python stdlib + one static
`index.html`. Three moving parts:

```
 phone/browser ──wss──▶  relay.atg.link  (AWS box: relay.py)  ◀──wss──  laptop: worker.py → harness :8787
 (fleet UI)              token + passkey gate, routes              (the real Claude sessions)
```

## The one UI, two modes
`index.html` is **mode-aware** via `window.__FLEET__`:
- **harness** serves it at `localhost:8787` untouched → **direct mode** (talks to the
  local harness; no machines rung, no passkey).
- **relay** injects `window.__FLEET__=true` (`relay.py` `_serve_file`) → **fleet mode**
  (machines rung + passkey + `toMachine` wrapping).

It's **one file** now — `clawd-harness/index.html` at the repo root. The harness serves
it directly; the relay serves the same file (`_serve_file` reads it from one level up,
`fleet/../index.html`) and injects the flag. No copy to keep in sync.

### Changing the UI
Edit the one canonical file, then ship it to the box:
```bash
# 1. edit ~/clawd/clawd-harness/index.html   (harness live-reloads localhost:8787)
# 2. ship to the box (relay serves it fresh per request — no restart needed)
scp ~/clawd/clawd-harness/index.html zkllmapi:~/clawd-fleet/index.html
```
Verify the JS first: extract the `<script>` and `node --check` it.

## The box (relay backend)
- Host: `ssh zkllmapi` (Ubuntu, `174.129.67.164`). **Shared/production** — also runs
  conclave, mediamtx, etc. Touch nginx carefully; `nginx -t` before any reload.
- Relay: `wss://relay.atg.link` → nginx → `127.0.0.1:8788`. Cert via certbot (auto-renew).
  Code at `~/clawd-fleet`. Service: `clawd-fleet-relay` (systemd). Logs:
  `journalctl -u clawd-fleet-relay -f`.
- **Ship relay/worker changes** (run from the harness root; fleet code is in `fleet/`,
  the shared UI at the root):
  `scp fleet/relay.py fleet/worker.py fleet/fleet_ws.py fleet/webauthn.py index.html favicon.png zkllmapi:~/clawd-fleet/`
  then `ssh zkllmapi 'sudo systemctl restart clawd-fleet-relay'`. The box stays **flat**,
  so `index.html` sits next to `relay.py` there (`_serve_file` checks `HERE/` first).
- `fleet.env` on the box (gitignored) holds: `FLEET_MOBILE_TOKEN`, `FLEET_WORKER_TOKEN`,
  `FLEET_WORKER_ALLOW`, `FLEET_RP_ID=relay.atg.link`, `FLEET_ORIGIN=https://relay.atg.link`,
  `FLEET_REQUIRE_PASSKEY=1`, `FLEET_ALLOW_ENROLL=0`.

## The worker (each machine)
The laptop runs `worker.py` as a launchd agent `com.clawd.fleet-worker`
(`~/Library/LaunchAgents/`), pointing `FLEET_RELAY=wss://relay.atg.link` and
`--harness ws://127.0.0.1:8787`. To add a machine: run `worker.py` there with the
worker token, and add its `--machine` id to `FLEET_WORKER_ALLOW` on the box.

## Auth (two factors)
1. **Mobile token** (`?t=`) — gates the relay handshake; stored in localStorage, stripped
   from the URL on load. Get it: `ssh zkllmapi 'grep ^FLEET_MOBILE_TOKEN= ~/clawd-fleet/fleet.env | cut -d= -f2'`.
2. **Passkey** (WebAuthn) — `webauthn.py` verifies the assertion against the enrolled
   credential (`.clawd-fleet.passkeys.json` on the box) and pins `rpId=relay.atg.link`.
   A success mints a 24h session.

**Enroll a new device** (passkeys sync via iCloud Keychain, so usually unnecessary):
set `FLEET_ALLOW_ENROLL=1`, restart the relay, enroll on the device, set it back to `0`.

## Tests (from `fleet/`, no browser)
`cd fleet && python3 test_webauthn.py` · `test_relay_passkey.py` · `fleet_smoke.py` · `fleet_proxy_smoke.py`
