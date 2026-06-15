# Deploy & operate clawd-fleet

No Vercel, no build step. The whole system is pure-Python stdlib + one static
`index.html`. Three moving parts:

```
 phone/browser ──wss──▶  h.atg.link  (AWS box: relay.py)  ◀──wss──  laptop: worker.py → harness :8787
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
- Relay: `wss://h.atg.link` → nginx → `127.0.0.1:8788`. Cert via certbot (auto-renew).
  Code at `~/clawd-fleet`. Service: `clawd-fleet-relay` (systemd). Logs:
  `journalctl -u clawd-fleet-relay -f`.
- **Ship relay/worker changes** (run from the harness root; fleet code is in `fleet/`,
  the shared UI at the root):
  `scp fleet/relay.py fleet/worker.py fleet/fleet_ws.py fleet/webauthn.py index.html favicon.png zkllmapi:~/clawd-fleet/`
  then `ssh zkllmapi 'sudo systemctl restart clawd-fleet-relay'`. The box stays **flat**,
  so `index.html` sits next to `relay.py` there (`_serve_file` checks `HERE/` first).
- `fleet.env` on the box (gitignored) holds: `FLEET_MOBILE_TOKEN`, `FLEET_WORKER_TOKEN`,
  `FLEET_WORKER_ALLOW`, `FLEET_RP_ID=h.atg.link`, `FLEET_ORIGIN=https://h.atg.link`,
  `FLEET_REQUIRE_PASSKEY=1`, `FLEET_ALLOW_ENROLL=0`.

## The worker (each machine)
The laptop runs `worker.py` as a launchd agent `com.clawd.fleet-worker`
(`~/Library/LaunchAgents/`), pointing `FLEET_RELAY=wss://h.atg.link` and
`--harness ws://127.0.0.1:8787`. To add a machine: run `worker.py` there with the
worker token, and add its `--machine` id to `FLEET_WORKER_ALLOW` on the box.

## Auth — passkey-only, one trust story
**There is no user token.** Your passkey is the sole user credential
(`FLEET_PASSKEY_ONLY=1`). The worker token still gates *machine registration*
(`FLEET_WORKER_TOKEN`) — that's a machine-fleet secret, not your login.

1. **Relay edge gate (passkey doorman).** A mobile connects with no token; the
   relay requires a passkey assertion before it reveals the roster. Anti-abuse +
   keeps the roster non-public; **not** the security boundary.
2. **End-to-end channel (the real boundary).** When you open a machine, the
   mobile and that machine's **worker** run a passkey-bound authenticated key
   exchange (`fleet-e2e/1`, see **E2E-PROTOCOL.md**): the worker independently
   verifies a channel-bound passkey (require-UV) against its **laptop-local**
   passkeys file, and all traffic is AES-GCM end-to-end. The relay only routes
   ciphertext, so a compromised relay is reduced to denial-of-service. The worker
   session slides on activity (`FLEET_E2E_IDLE_TTL`, 10 min) with a hard ceiling
   (`FLEET_E2E_MAX_TTL`, 1 h).

One trust story: **the laptop is the authority.** It accepts only the passkey in
its own local file (it never trusts the relay for the pubkey). Box access alone
can't add a credential the worker will accept — you also need laptop file access.

### Provisioning the passkey (no web enrollment)
There is **no enrollment endpoint** — nothing network-reachable can add a
credential. The passkey public key is provisioned by an admin. A passkey is
rpId-bound to `h.atg.link`, so a brand-new credential must be **created at the
h.atg.link origin** (a deliberate act): temporarily restore a register endpoint
(it lives in git history), create via Face ID, then remove it again. In practice
you rarely need this — **iCloud Keychain syncs your existing passkey** to new
Apple devices automatically. After (re)provisioning, copy the *public* credential
to each worker (the file holds only public keys):
```bash
scp zkllmapi:~/clawd-fleet/.clawd-fleet.passkeys.json ~/clawd/clawd-harness/fleet/
launchctl kickstart -k gui/$(id -u)/com.clawd.fleet-worker   # reload to pick it up
```
The worker prints its identity fingerprint + passkey count at startup; the
browser shows the worker fingerprint on first pin (verify it once).

## Tests (from `fleet/`, no browser)
`cd fleet && python3 test_webauthn.py` · `test_relay_passkey.py` · `fleet_smoke.py`
· `fleet_proxy_smoke.py` · `test_e2e.py` · `test_e2e_mitm.py` · `test_e2e_interop.py` (needs `node`)
