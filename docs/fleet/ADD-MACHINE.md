# Add a machine to the fleet — a self-contained checklist

**Goal:** get a new machine running its own [clawd-harness](../../README.md) **and**
a fleet **worker**, so it shows up as a machine in the phone UI
(`https://h.atg.link/?t=<TOKEN>`) next to the others and you can drive its Claude
sessions from there — and, optionally, so the fleet **PM brain** can see and drive
it too (**step 8**).

This is the doc to hand a fresh Claude on the new machine. Follow it top to
bottom. It is written to be complete: it calls out the two pieces that are **not
in the repo** (and can't be — they're per-deployment secrets) and exactly where
to get them.

## The model (why no firewall changes are needed)

Everything **dials out** to one public **relay** (`wss://h.atg.link`, already
live). The new machine runs:

- a **harness** (`server.py`) on `127.0.0.1:8787` — the thing that actually runs
  `claude` in PTYs (projects, sessions, transcripts), and
- a **worker** (`fleet/worker.py`) — a small agent that dials the relay, registers
  this machine, and bridges the phone ⇄ the local harness.

The worker only makes outbound connections, so the new machine needs **no inbound
ports and no firewall changes**, even behind NAT. See
[`ARCHITECTURE.md`](ARCHITECTURE.md) for the full design.

```
 📱 phone ──wss──▶ relay (wss://h.atg.link) ──▶ worker (this machine) ──▶ harness (127.0.0.1:8787) ──▶ claude
```

---

## Before you start: two secrets you must bring with you

These are **gitignored on purpose** — they are not in the repo and never should
be. Get them from an **existing fleet machine** (e.g. the laptop, where they live
under `fleet/`) or from the operator. You need **both**:

| What | Env / file | Where to get it |
|---|---|---|
| **Worker token** | `FLEET_WORKER_TOKEN` | The box's `~/clawd-harness/fleet/fleet.env` (`grep WORKER_TOKEN`), or an existing machine's worker config (e.g. the laptop's launchd plist). Use the **worker** token, *not* the mobile/URL token. |
| **Passkey file** | `fleet/.clawd-fleet.passkeys.json` | The canonical copy lives on the box: `scp zkllmapi:~/clawd-harness/fleet/.clawd-fleet.passkeys.json` (or copy from any existing machine's `fleet/` dir). It holds only **public** keys, is the **same on every machine**, and is what lets this worker verify *you* over the end-to-end channel. See [`DEPLOY.md`](DEPLOY.md) → "Provisioning the passkey". |

You also need, for **step 4**, the ability to edit the relay's allowlist on the
box (`ssh zkllmapi`) — or ask whoever has it to add this machine's id.

> **Do NOT copy `fleet/.fleet.worker_id.json`.** That's this machine's *own*
> long-term identity key — it is generated fresh per machine on first run, and the
> phone trust-on-first-use pins it. Copying another machine's identity would make
> two machines claim the same identity. Only the **passkeys** file is shared.

---

## Step 1 — clone the repo and run the harness

The repo is **public**, so no auth is needed to clone:

```bash
git clone https://github.com/clawdbotatg/clawd-harness.git ~/clawd/clawd-harness
cd ~/clawd/clawd-harness
```

Install the harness as a daemon. It needs the `claude` CLI signed into a Claude
**subscription** (OAuth) — run `claude` once interactively first to log in if you
haven't.

```bash
./daemon.sh install        # launchd on macOS; re-resumes sessions on reboot
```

Confirm it's listening:

```bash
lsof -nP -iTCP:8787 -sTCP:LISTEN     # should show python on :8787
```

The harness writes a token to `.clawd-harness.token`; the worker auto-discovers
it, so you don't pass it by hand.

### Step 1b — create `.clawd-harness.env` (AI naming + TTS keys)

**Easy to miss, and the usual cause of "session auto-naming / summaries stopped
working."** The harness reads per-deployment secrets from a **gitignored**
`.clawd-harness.env` at the repo root. It does **not** come across in the clone
(by design — it holds keys), so a fresh machine has none and the harness silently
falls back to dumb first-prompt titles. The boot log says so:
`note: AI naming off (set BANKR_API_KEY + BANKR_BASE_URL …)`.

Copy it from an existing machine (it's the same secrets everywhere):

```bash
scp zkllmapi:~/clawd/clawd-harness/.clawd-harness.env ~/clawd/clawd-harness/
# (or from the laptop / any working harness)
chmod 600 ~/clawd/clawd-harness/.clawd-harness.env
```

Or create it by hand — minimum for AI session naming, plus optional ElevenLabs TTS:

```bash
# AI session naming via the Bankr OpenAI-compatible gateway.
# BANKR_API=bankr → auth via the X-API-Key header.
BANKR_API=bankr
BANKR_BASE_URL=https://llm.bankr.bot/v1
BANKR_MODEL=qwen3-coder
BANKR_API_KEY=<bankr-key>

# Optional: ElevenLabs TTS (Flash v2.5). Without it the browser uses the native voice.
ELEVENLABS_API_KEY=<elevenlabs-key>
ELEVENLABS_VOICE_ID=<voice-id>
```

`.clawd-harness.env` is in the harness's `RESTART_FILES`, so **saving it triggers
a graceful self-restart** (after in-flight turns finish) and the daemon loads the
keys — no manual restart needed. Verify naming is live:

```bash
cd ~/clawd/clawd-harness && python3 -c "import server; print('naming on:', bool(server.BANKR_API_KEY and server.BANKR_BASE_URL))"
```

## Step 2 — install the worker's one dependency: `cryptography`

The harness and relay are pure stdlib, but the **worker needs
[`cryptography`](https://pypi.org/project/cryptography/)** (pyca) for the
end-to-end encrypted channel between the phone and this machine. This is the
single most common reason a new machine "joins but won't open" — see
Troubleshooting.

```bash
python3 -m pip install cryptography        # or: pipx / your venv of choice
```

Verify the worker can import its E2E module:

```bash
cd fleet && python3 -c "import e2e; print('e2e OK')"
```

If that prints `e2e OK`, you're good. If it raises `ModuleNotFoundError:
cryptography`, the install didn't land in the Python that will run the worker —
fix that before continuing (the daemon must use the **same** `python3`).

## Step 3 — drop in the two secrets

From the values you brought in "Before you start":

```bash
# worker token: keep it out of shell history / the repo. You'll put it in the
# launchd plist (step 6) or export it for the step-5 test.

# passkey file: copy it into THIS repo's fleet/ dir, verbatim, then lock it down.
# canonical source is the box (holds only public keys):
scp zkllmapi:~/clawd-harness/fleet/.clawd-fleet.passkeys.json ~/clawd/clawd-harness/fleet/
# (or from any existing machine's fleet/ dir, e.g. the laptop)
chmod 600 ~/clawd/clawd-harness/fleet/.clawd-fleet.passkeys.json
```

Sanity check it's valid JSON with at least one credential:

```bash
cd ~/clawd/clawd-harness/fleet && python3 -c "import json;print(len(json.load(open('.clawd-fleet.passkeys.json'))),'passkey(s)')"
```

> The relay's `RP_ID`/`ORIGIN` default to `h.atg.link` / `https://h.atg.link`,
> which is correct for the production relay — **no env needed**. Only set
> `FLEET_RP_ID` / `FLEET_ORIGIN` if you point this worker at a *different* relay
> domain.

## Step 4 — allowlist this machine on the relay (needs box access)

Pick a unique machine id for this box (e.g. `austin-desktop`). The relay only
accepts workers whose id is in `FLEET_WORKER_ALLOW`. Add it on the box:

```bash
# from a machine that can ssh to the box (e.g. the laptop), append the new id:
ssh zkllmapi 'grep FLEET_WORKER_ALLOW ~/clawd-harness/fleet/fleet.env'    # see current list
# edit fleet.env to add ",austin-desktop" to the FLEET_WORKER_ALLOW line, then:
ssh zkllmapi 'sudo systemctl restart clawd-fleet-relay'
```

If you don't have box access, ask the operator to add your chosen id. (If the
allowlist is empty it means allow-any, and you can skip this — but production
keeps it set.)

## Step 5 — test the worker in the foreground

Before daemonizing, confirm it registers and the E2E module is live:

```bash
cd ~/clawd/clawd-harness/fleet
FLEET_RELAY=wss://h.atg.link FLEET_WORKER_TOKEN=<worker-token> \
  python3 worker.py --machine austin-desktop --host "$(hostname)" \
  --harness ws://127.0.0.1:8787
```

Healthy output includes a line like
`[worker austin-desktop] E2E identity <fp> · 1 passkey(s) enrolled` and **no**
`E2E required but unavailable (cryptography missing)` or `no passkeys enrolled`
warning. (`0 passkey(s) enrolled` means step 3 didn't land — the proxy will refuse
every viewer.) Leave it running and do step 7 to confirm end-to-end, then `Ctrl-C`
and daemonize (step 6).

> If you see `⚠ FLEET_E2E_REQUIRE=0 — end-to-end encryption disabled`, that means
> someone turned the boundary off. **Don't** do that to "make it work" — the
> missing piece is almost always `cryptography` (step 2) or the passkey file
> (step 3). Leave `FLEET_E2E_REQUIRE` at its default (`1`).

## Step 6 — make it permanent

### macOS (launchd)

Save as `~/Library/LaunchAgents/com.clawd.fleet-worker.plist` (substitute your
home dir, machine id, and the worker token):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.clawd.fleet-worker</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/python3</string>
    <string>/Users/<you>/clawd/clawd-harness/fleet/worker.py</string>
    <string>--machine</string><string>austin-desktop</string>
    <string>--host</string><string>desktop</string>
    <string>--harness</string><string>ws://127.0.0.1:8787</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/<you>/clawd/clawd-harness/fleet</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>FLEET_RELAY</key><string>wss://h.atg.link</string>
    <key>FLEET_WORKER_TOKEN</key><string><worker-token></string>
    <key>HARNESS_WS</key><string>ws://127.0.0.1:8787</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/<you>/Library/Logs/clawd-fleet-worker.log</string>
  <key>StandardErrorPath</key><string>/Users/<you>/Library/Logs/clawd-fleet-worker.log</string>
</dict>
</plist>
```

The `python3` you point at **must** be the one that has `cryptography` installed
(step 2). Then:

```bash
chmod 600 ~/Library/LaunchAgents/com.clawd.fleet-worker.plist     # it holds the token
launchctl load -w ~/Library/LaunchAgents/com.clawd.fleet-worker.plist
tail -f ~/Library/Logs/clawd-fleet-worker.log
```

Manage it later: `launchctl list | grep fleet-worker` (running?),
`launchctl unload …plist` (stop), `launchctl load -w …plist` (start).

### Linux (systemd)

Use [`../../fleet/deploy/clawd-fleet-worker.service`](../../fleet/deploy/clawd-fleet-worker.service)
as a template. Set `WorkingDirectory` to this repo's `fleet/`, `ExecStart` to
`python3 worker.py --machine <id> --host <host> --harness ws://127.0.0.1:8787`,
and an `EnvironmentFile` holding `FLEET_RELAY` + `FLEET_WORKER_TOKEN`. Make sure
the unit's `python3` has `cryptography`. Then:

```bash
sudo cp fleet/deploy/clawd-fleet-worker.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now clawd-fleet-worker
journalctl -u clawd-fleet-worker -f
```

## Step 7 — verify from the phone

Open `https://h.atg.link/?t=<MOBILE_TOKEN>` (the mobile token, factor 1) and pass
the passkey prompt (factor 2). The **machines** rung should now list your new id
(e.g. `austin-desktop`) as online. Tap it:

- its local **projects/sessions load** → the worker↔harness bridge works;
- **open a session** and watch the terminal/transcript update live → the
  end-to-end channel works.

That last step is the real test: a machine that appears in the roster but won't
open a session is the classic "worker registered but E2E isn't up" symptom — go
to Troubleshooting.

On the box you can confirm the join: `ssh zkllmapi 'journalctl -u
clawd-fleet-relay | grep -E "online|offline" | tail'`.

## Step 8 — (optional) let the fleet PM brain drive this machine

The fleet has an AI **project-manager brain** (the controller, see
[`../CONTROLLER.md`](../CONTROLLER.md)) running on the box. It can see and drive
**every** machine's sessions — you chat with it at `https://h.atg.link` (tap the
status chip → PM drawer) or over Telegram, and it can triage, start work, and
unblock sessions across the fleet.

It reaches machines over a **trusted-control path** that is *separate from* the
phone's end-to-end channel: the box-resident controller connects to the relay as a
trusted `controller` role, and an opted-in worker bridges its (plaintext) control
frames to the local harness. The trade is explicit — **a compromised box could
drive machines** — but the phone⇄worker E2E traffic stays encrypted and unreadable
to the relay/box regardless. So it's **opt-in per machine**.

The box side is already set up (the relay holds `FLEET_CONTROLLER_TOKEN` and the
controller runs in box mode). The **only per-machine step** is to opt this worker
in by setting **`FLEET_CTL_ALLOW=1`** in its environment, then restart it. Put it
wherever this machine's worker reads env so it persists:

- **launchd (macOS):** add to the plist's `EnvironmentVariables` dict
  `<key>FLEET_CTL_ALLOW</key><string>1</string>` (or to a `fleet/fleet.env` the
  worker loads), then
  `launchctl kickstart -k gui/$(id -u)/com.clawd.fleet-worker`.
- **systemd (Linux):** add `FLEET_CTL_ALLOW=1` to the worker's `EnvironmentFile`
  (e.g. `fleet.env`), then `sudo systemctl restart clawd-fleet-worker`.

With it set, the worker accepts the box brain's trusted control even though E2E
stays **required** for phones (the worker uses the plaintext reply path only for
the reserved controller identity; everything else still goes through E2E).

**Verify** the brain can see this machine — from a shell with box access:

```bash
ssh zkllmapi 'curl -s http://127.0.0.1:8799/api/world' \
  | python3 -c "import sys,json;[print(m['id'],'sessions='+str(m['session_total'])) for m in json.load(sys.stdin)['machines']]"
```

This machine should show its real `session_total` (not `0`). Or just ask the PM
at h.atg.link: *"what's on `<your-machine-id>`?"*. The controller re-pulls a
machine's projects/sessions automatically whenever its worker (re)connects, so no
controller restart is needed.

> **Updating an existing machine's worker code** (e.g. to gain trusted-control
> support): every machine — worker laptops **and the box** — is now a **git
> checkout** that pulls from `origin/main`, so `git pull --ff-only` + restart the
> worker (NOT scp, which dirties the checkout). The box checkout is at
> `~/clawd-harness`. See the memory note "fleet-machine-update" for the per-machine
> ssh aliases/paths.

---

## Troubleshooting

**Machine shows in the roster but opening it / a session hangs or errors.**
The worker registered (token + allowlist OK) but the end-to-end channel can't
come up. In order of likelihood:
1. **`cryptography` not installed** in the worker's Python (step 2). Check the
   worker log for a `cryptography missing` / `E2E unavailable` line. With
   `FLEET_E2E_REQUIRE=1` (default) the worker **refuses to proxy** without it, so
   the machine looks online but won't open. Fix: install `cryptography` into the
   exact `python3` the daemon runs, restart the worker.
2. **Passkey file missing or wrong** (`fleet/.clawd-fleet.passkeys.json`, step 3).
   The worker can't verify your passkey, so the channel handshake fails. Copy the
   file from a working machine; confirm it parses (step 3's one-liner).
3. **`RP_ID`/`ORIGIN` mismatch** — only if you set them. They must match the relay
   domain you actually connect through. Unset = `h.atg.link` (correct for prod).

**Machine never appears in the roster.**
- Wrong/old **worker token**, or this machine's id isn't in the relay
  **allowlist** (step 4) — the relay rejects the registration. Check the worker
  log for an auth/`rejected` line; check `FLEET_WORKER_ALLOW` on the box.
- The worker can't reach the relay (no outbound internet / DNS). Test:
  `python3 -c "import socket; socket.create_connection(('h.atg.link',443),5); print('ok')"`.

**Sessions are empty / `claude` isn't running.**
That's a harness problem, not fleet. Make sure step 1 worked (`lsof` shows
`:8787`) and `claude` is logged into a subscription. See the root
[`CLAUDE.md`](../../CLAUDE.md).

**The PM brain lists this machine but shows `sessions: 0` for it** (phone access
works, but the brain can't see its projects/sessions). The worker isn't opted into
the trusted-control path — set `FLEET_CTL_ALLOW=1` in its env and restart it
(**step 8**). Until then the worker refuses the box brain's control frames (E2E is
required for everyone else), so only the aggregate roster entry shows. Confirm the
worker reconnected in the relay log, then re-check `…:8799/api/world`.

**Session titles are dumb first-prompt strings / auto-naming stopped.**
The harness is missing `.clawd-harness.env` (or its `BANKR_*` keys) — see
**step 1b**. Confirm with the boot log (`AI naming off …`) or the one-liner in
that step. Same file holds the ElevenLabs key, so missing TTS has the same fix.

**`pkill -f worker.py` over SSH kills your shell** (the pattern matches its own
command line). Use the bracket trick `pkill -f "[w]orker.py"`, or just
`systemctl restart` / `launchctl`.

---

## What's new vs. the old recipe

Earlier docs (the short "Add a new machine" snippet in
[`RUNBOOK.md`](RUNBOOK.md)) predate the **end-to-end encryption** layer and
omitted steps 2 and 3 — which is exactly why a from-scratch attempt would join the
roster but fail to open a machine. This page is the current, complete path; the
RUNBOOK now points here.
