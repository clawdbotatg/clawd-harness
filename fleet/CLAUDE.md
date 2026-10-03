# fleet/ — orientation for Claude

Drive N machines (each running a harness) from one phone through one public
relay. Full notes and history: `../docs/fleet/NOTES.md`. Design:
`../docs/fleet/ARCHITECTURE.md`. Wire contract: `../docs/WS-PROTOCOL.md`.
Shipping rules: root `../CLAUDE.md` (small change = push + check live).

## Rule that must not regress
**`fleet/` never imports or edits the harness.** `../server.py` is a black box
reached over its localhost WebSocket. Fleet fixes belong in the worker or the
relay. Everyone dials out to the relay; nothing accepts inbound connections.

## Pieces
- **relay.py** — public hub on the AWS box (`ssh zkllmapi`, `wss://h.atg.link`
  → nginx → `127.0.0.1:8788`). Routes JSON + binary PTY frames by machine/mobile
  id, serves `../index.html` with `window.__FLEET__` injected.
- **worker.py** — one per machine. Dials the relay, opens one harness
  connection per viewer (`HarnessLink`), pumps frames both ways. Config from
  gitignored `fleet.env`.
- **fleet_ws.py** — stdlib RFC 6455 helpers. Pure stdlib everywhere; only the
  worker needs `cryptography`.
- **../index.html** — the one UI; fleet mode adds the `machines` rung. Search
  `hsend`, `currentMachine`, `renderMachines`.

## Auth (what matters)
- **E2E channel is the security boundary** (`e2e.py`,
  `../docs/fleet/E2E-PROTOCOL.md`): passkey-bound key exchange with each
  worker, AES-GCM, relay sees ciphertext. The relay edge passkey gate is
  anti-abuse only. Don't call the relay "DoS only": it serves the UI and hosts
  the PM.
- Passkey cadence is pinned in code (`buildinfo.CADENCE`, 7 days) — not per-box
  config. `test_passkey_ttl.py`.
- Workers report `build:{code,ttl,...}`; `tools/shipcheck.py` uses it to say
  whether each box runs HEAD.
- Relay sends `prefs` (switched-off machines) BEFORE the first roster, or the
  page unlocks boxes you turned off.
- The passkey modal has two owners (edge gate + each handshake); every
  `hidePasskey()` names its owner.
- **Never casually restart workers** — e2e resume is on disk now, but each
  forced re-auth costs Austin a passkey.

## Deploy / ops
- Box is a git checkout at `~/clawd-harness`. UI-only changes need nothing
  past the pull. relay.py / worker.py on the box:
  `ssh zkllmapi 'cd ~/clawd-harness && git pull && sudo systemctl restart clawd-fleet-relay clawd-fleet-worker'`.
  Logs: `journalctl -u clawd-fleet-relay -f`.
- Box is shared (other sites on it): only add nginx vhosts, `nginx -t` first.
- `pkill -f "[w]orker.py"` — without the brackets it kills your ssh shell.
- New machine: `../docs/fleet/ADD-MACHINE.md`. Ops: `../docs/fleet/RUNBOOK.md`.
- Skill library + doc shelf on the relay: `../docs/fleet/SKILLS.md`,
  `../docs/fleet/DOCS-STORE.md`.
- Never commit `.clawd-fleet.*`, `fleet.env`, `*.log`.
