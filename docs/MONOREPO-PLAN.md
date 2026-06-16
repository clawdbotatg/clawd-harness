# Monorepo consolidation plan — fold clawd-fleet into clawd-harness

**Self-contained: executable without prior conversation context.** Delete this file when done.

## Goal
One repo (`clawd-harness`) holds the engine **and** the fleet (relay/worker). Kill the
only real duplication — `index.html` is currently byte-identical in both repos. The
runtime boundary stays a **code-discipline rule**: `fleet/` code must never import
`server.py` or reach into harness internals; the worker remains just a WS client.

## Current layout (before)
- `~/clawd/clawd-harness/` — engine (`server.py`) + `index.html` (the shared UI). github `clawdbotatg/clawd-harness`.
- `~/clawd/clawd-harness/projects/clawd-fleet/` — relay/worker/etc. A **separate** git repo (`clawdbotatg/clawd-fleet`) nested in the harness's gitignored `projects/`.
- Box: `ssh zkllmapi`, `~/clawd-fleet/` (scp'd copy), systemd `clawd-fleet-relay` → nginx → `relay.atg.link`.
- Laptop worker: launchd `com.clawd.fleet-worker` (`~/Library/LaunchAgents/`), runs `…/projects/clawd-fleet/worker.py`.

## Target layout (after)
```
clawd-harness/
  server.py            # engine, unchanged
  index.html           # THE single UI (harness serves it direct; relay serves it + injects __FLEET__)
  favicon.png
  fleet/               # moved from clawd-fleet (relay/worker layer)
    relay.py worker.py fleet_ws.py webauthn.py fleet_cli.py
    fleet_smoke.py fleet_proxy_smoke.py test_webauthn.py test_relay_passkey.py
    CLAUDE.md          # the relay-oriented orientation (from clawd-fleet/CLAUDE.md)
    deploy/            # systemd units, setup_tls.sh, fleet.env.example
  docs/fleet/          # DEPLOY.md ARCHITECTURE.md RUNBOOK.md HARNESS-PROXY.md (from clawd-fleet/docs/)
```

## Steps

**1. Move the code.** Into `~/clawd/clawd-harness/fleet/`: `relay.py worker.py fleet_ws.py
webauthn.py fleet_cli.py fleet_smoke.py fleet_proxy_smoke.py test_webauthn.py
test_relay_passkey.py deploy/`. Move `clawd-fleet/CLAUDE.md` → `fleet/CLAUDE.md`,
`clawd-fleet/docs/*` → `clawd-harness/docs/fleet/`. **Do NOT copy** `index.html`/`favicon.png`
(the harness's are already byte-identical — they become the one source). Merge
`clawd-fleet/.gitignore` runtime entries into the harness `.gitignore`
(`.clawd-fleet.token`, `.clawd-fleet.machine`, `.clawd-fleet.passkeys.json`, `fleet.env`).

**2. Point the relay at the shared `index.html`.** `relay.py` `_serve_file` reads `HERE / name`
(`HERE` = relay dir). The shared UI now lives one level up. Make it robust for BOTH the
monorepo (`../index.html`) and the flat box layout: serve the **first existing** of
`HERE/index.html`, `HERE.parent/index.html`. Keep the `__FLEET__` injection + favicon the same way.
Update the test that pre-seeds files if needed; re-run all 4 tests from `fleet/`.

**3. Commit to clawd-harness** (git identity `clawdbotatg` / `clawd@buidlguidl.com`, HTTPS).
Fleet git history isn't carried over — it's preserved at `github.com/clawdbotatg/clawd-fleet`
(archived in step 7). Scan the diff for secrets (gitleaks hook runs).

**4. Box deploy.** Keep `~/clawd-fleet/` on the box **flat** (relay.py, worker.py, fleet_ws.py,
webauthn.py, **index.html**, fleet.env, deploy units). Because step 2 also checks `HERE/index.html`,
the flat layout still works. Re-scp those files from `clawd-harness/fleet/` (+ `index.html` from root),
`sudo systemctl restart clawd-fleet-relay`. Verify `relay.atg.link` 200 + `__FLEET__=true` injected.

**5. Repoint the laptop worker.** Edit `~/Library/LaunchAgents/com.clawd.fleet-worker.plist`:
ProgramArguments `…/clawd-harness/fleet/worker.py`, WorkingDirectory `…/clawd-harness/fleet`.
`launchctl unload … && launchctl load …`. Confirm the relay logs `worker online: austin-laptop`.

**6. Remove the old nested checkout** `~/clawd/clawd-harness/projects/clawd-fleet/` once the
monorepo works (it's gitignored by the harness).

**7. Archive the GitHub repo** `clawdbotatg/clawd-fleet` (read-only; history preserved):
`gh repo archive clawdbotatg/clawd-fleet --yes`. Optionally add a README note → moved into clawd-harness.

**8. Docs.** clawd-harness `CLAUDE.md`: add a `fleet/` section (it's part of this repo now; keep the
no-import discipline note). The cp→scp **UI sync ritual is GONE** — one `index.html`; just edit it +
scp to the box. Update `docs/fleet/DEPLOY.md` paths. Update the memory `clawd-fleet-live-state`.

## Verify (done when all true)
- `localhost:8787` → 200, direct mode (no `__FLEET__` injection).
- `relay.atg.link` → 200, fleet mode (`__FLEET__=true` injected).
- 4 tests pass from `fleet/`. Relay active; `austin-laptop` + `clawd-nerve-cord` online.
- `diff` proves there's now **one** `index.html` (no second copy).

## Decision already made
Going monorepo (Austin's call). The standalone-harness concern is waived.
