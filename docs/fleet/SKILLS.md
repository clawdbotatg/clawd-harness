# Fleet skills — the private skill library (handoff doc)

Read this before touching the 📚 picker, `skillput`, the relay's `/skills/*`,
or the `add-skill` skill. It is the whole feature: what it is, how a skill
travels, why it's built this way, where secrets end up, every knob, every
landmine, and how to test it. History with dates lives in
[`../HISTORY.md`](../HISTORY.md) (2026-08-30, 2026-09-04 entries).

## What it is, in one paragraph

One stack of **user-written skill files**, stored on the relay box, shown as
one list on every machine and device via the harness **📚 button** (right end
of the quick-chip strip, right of 🕘, visible inside a session). Tap a skill →
it **attaches to your next message** as a 📚 chip above the composer, exactly
like a dropped `.md` file. Keep typing. Enter sends your text, then one line
per skill on its own line:

    Use the "vision" skill: read /…/.clawd-harness-uploads/paste-ab11ce05-vision-SKILL.md and follow it. If it needs details I haven't given, ask me.

Claude Reads the file and follows it. That's the product: Austin writes down
how to do a thing once (drive the 3D printer, see through the cameras, post to
the Vestaboard), and hands it to any session on any machine with one tap —
and the tap itself never fires anything off.

## Two rules that shaped it

1. **The library is decoupled from machines.** It never scans, installs to, or
   deletes from `~/.claude/skills/` anywhere. The first build (2026-08-30)
   synced skills onto every box and was ripped out the same day; a one-shot
   janitor in `fleet/worker.py` (`_skills_sync_cleanup`) removes what it had
   installed. A skill reaches a session only as a file in that machine's
   upload dir plus the pointer line. Don't rebuild the sync. Don't write
   skills into a project's `.claude/skills/` either — same objection, and it
   breaks under codex.
2. **A picked skill IS a dropped `.md`.** The tap reuses the attachment
   pipeline wholesale instead of growing its own. Everything a dropped file
   has (hold-on-Enter mid-upload, red chip on failure, fleet upload bridge,
   mixing with images, picking from the sessions rung) is inherited, and
   there is one behavior to understand and one probe to keep green. The
   2026-08-30 build pasted the whole SKILL.md into the PTY on tap; Austin's
   complaint ("it just spews it into the session") led to the 2026-09-04
   redesign. The pointer line was chosen over pasting the body under the
   text: one short line of context per pick and a readable transcript.

## How a skill travels

```
skillput <dir>  ──POST /skills/put──▶  relay box: fleet/.clawd-fleet.skills/<name>/
   (worker token)                      gitignored; whole-dir swap; ✕ → .trash/<name>-<epoch>/
                                                  │
📚 open → {type:"skillsLib"} ─────────────────────┤  fleet mode: phone → relay socket (passkey-authed)
   reply {skills:[{name,description,body}]}       │  direct mode: browser → harness → relay https
                                                  ▼                        (serve_skills_lib, worker token)
tap row → attachSkill(s):  body → File("<name>-SKILL.md", text/markdown)
        → uploadFile(file, {glyph:'📚', label:name, fold})       ← same call a dropped .md makes
        → POST /upload  (direct: local harness; fleet: relay → that machine's worker → its harness)
        → {path,name} → chip {path, label, glyph, fold: skillInvocation(name, path)}
Enter → composeSend(text, ready):  "<text> <plain file paths>⏎<fold line>⏎<fold line>"
        → deliverSend → PTY as one bracketed paste → claude Reads the path
```

### Store (`fleet/relay.py`)

- Dir: `.clawd-fleet.skills/` next to `relay.py`, one subdir per skill,
  gitignored (`fleet/.clawd-fleet.skills*`). Names `[a-z0-9._-]`, relpaths
  fenced, ≤64 files / ≤4 MB per skill, whole-dir swap on publish so a
  half-uploaded skill is never served.
- HTTP, all gated by the **worker token** (`_token_ok` = `hmac.compare_digest`,
  no timing oracle; a loud "dev default" warning prints if no token is
  configured): `GET /skills/manifest`, `GET /skills/get?name=` (files come
  back **base64** in a `{path: b64}` dict), `GET /skills/lib` (bodies
  decoded, the picker payload), `POST /skills/put` (publish, or
  `{name, delete:true}`).
- WS (fleet mode): `{type:"skillsLib"}` and `{type:"skillsRm", name}` on the
  mobile socket. The relay refuses every frame except `ping` until
  `_mobile_authed` — passkey satisfied and unexpired, re-checked per frame.
  Both reply `{type:"skillsLib", skills:[…], error?}`.
- **Remove = trash.** ✕ (behind a `confirm()`) moves the dir to
  `.clawd-fleet.skills/.trash/<name>-<epoch>/` — a dot-dir, never listed, so
  a fat-fingered ✕ is recoverable by an admin on the relay box.

### Direct-mode proxy (`server.py`, `serve_skills_lib`)

Threaded (network I/O off the WS reader loop). Config from
`_skills_relay_cfg()`: `FLEET_RELAY` + `FLEET_WORKER_TOKEN`/`FLEET_TOKEN`,
from the environment or the checkout's `fleet/fleet.env`; `wss://` is
rewritten to `https://`. Unconfigured → the picker shows "no relay configured
on this machine — open h.atg.link instead".

**Landmine:** on the dev Mac (`~/clawd-harness`) `fleet/fleet.env` has NO
relay line, so direct-mode 📚 and `skillput` both fail there, while fleet
mode works because the phone talks to the relay directly. The fleet worker
gets its relay + token from its launchd plist
(`~/Library/LaunchAgents/com.clawd.fleet-worker.plist`,
`EnvironmentVariables`). To publish from that box without editing config,
export those two vars from the plist into the shell (a python one-liner with
`plistlib` + `shlex.quote`; never print the token) and run `skillput`.
Adding `FLEET_RELAY=wss://h.atg.link` + `FLEET_WORKER_TOKEN=…` to that
box's `fleet/fleet.env` would fix it for good — a config edit, Austin's call.

### Publish (`share/bin/skillput`)

In `~/bin` on every fleet box via the shared-kit sync. `skillput <dir>` /
`skillput list` / `skillput rm <name>`. There is no `skillput get` — to read
a skill's text, hit `/skills/get` and base64-decode, or open the picker.

### UI (`index.html`, the `📚 skill book` section + the attachments block)

- `showSkillbook()` fetches fresh on every open; `renderSkillbook(skills,
  error)` rebuilds the modal per reply (on-demand modal → wholesale rebuild
  is fine here; stale replies after close are dropped).
- `skillSendable()`: in a session (`tty`/`transcript` with a cid) or on a
  project's sessions rung (`sessions` with a pid), and in fleet mode only
  with a `currentMachine` (uploads need a target). Otherwise the modal shows
  "open a project or session first" and rows are no-ops.
- `attachSkill(s)`: dedupes by label (one chip per skill), wraps the body in
  a `File`, calls `uploadFile(file, {glyph, label, fold})`. Then the modal
  closes and **`box.focus()`** — you came from the box, you go back to it.
- Attachment slots carry optional `glyph` (rendered where an image thumbnail
  would be, CSS `.chip .glyph`), `label` (chip text + pending-box label) and
  `fold` (set after upload from `opts.fold(path)`). `composeSend(text,
  ready)` is the ONE place a message is assembled: text + plain paths joined
  by spaces, then each `fold` on its own line. Both `dispatchSend` and the
  navigated-away branch of `holdSend` go through it — never assemble the
  message by hand elsewhere.
- `skillInvocation(name, path)` is the pointer line. It was dead code from
  the 08-30 build (written for the sync design, never called) and was
  revived unchanged.
- Empty box + Enter = the old one-tap flow (just the pointer line).
- Chips don't survive a reload, same as any attachment. Re-pick.

## Where a secret goes (audited 2026-09-04)

The `add-skill` skill used to say "NEVER put credentials in the skill". That
rule dated from the paste-into-PTY era and is **reversed**: credentials
belong in library skills. The audit, so you don't redo it:

| Hop | Protection |
|---|---|
| Store on the relay box | gitignored dir; box is Austin's |
| HTTP `/skills/*` | worker token, constant-time compare |
| Phone picker socket | passkey (Face ID) per session, re-checked every frame, expires |
| Wire | TLS at h.atg.link (relay speaks plain ws behind the terminator); direct-mode harness → relay is https |
| Browser | bodies in memory while the modal is open; **not** in localStorage (🕘 sent history records only the sent text = the pointer line) |
| Harness prompt log `.clawd-harness.prompts.jsonl` | pointer line only |
| Target machine | `.clawd-harness-uploads/paste-<hex>-<name>-SKILL.md`, gitignored, mode 644 in a single-user account, never cleaned up (same as any dropped file) |
| Session transcript | the body, once claude Reads it — the same footprint as any secret a session reads |
| Anthropic API | the body, as part of the Read tool result — same as any file claude reads; the 🟦 tee proxy never logs |

Net: the new flow is *safer* than the old paste — the body no longer lands in
the prompt log. The only open footprint is the transcript + upload file on
the one machine you used it on. Weak spots worth knowing: direct mode over a
LAN URL is plain http (use 127.0.0.1 or h.atg.link); if claude echoes a token
in a reply, the 🟦 summary and 🔊 voice will repeat it (so skills tell the
reader not to echo secrets).

Habits the `add-skill` skill now teaches: one secret per line (rotation = one
edit + republish), tell the reader not to echo it, write the skill in a
private working dir and delete it after publishing. The `vision` skill is the
live example — it carries the Reolink Hub password.

## Writing a skill

The `add-skill` skill in the library is the canonical how-to and is kept in
sync with this doc — if you change the flow, republish `add-skill` too. Short
version: a dir named for the skill holding `SKILL.md` (`---\nname: …\n
description: …\n---` frontmatter, then instructions a session with zero
context could follow), test it against the real device, `skillput <dir>`.
The `description` is what the picker shows. Everything the session needs
goes in the body; the body is all it gets.

## Tests

- `fleet/test_skills_lib.py` — store + library end to end: auth, hostile-path
  fences, bodies over HTTP and WS, trash-on-remove, the sync janitor.
- `test_skills_frame.py` — the direct-mode harness proxy (real relay behind it).
- `tools/skillbookprobe.mjs` — the 📚 modal with real touch gestures and real
  keystrokes, `hsend` and the `/upload` fetch stubbed: fetch on open; a tap
  uploads the body as `text/markdown; name="<name>-SKILL.md"` and attaches a
  📚 chip with nothing sent, the box focused; a second tap dedupes; typing +
  📤 sends ONE `send` frame = `text⏎pointer line`; the chip ✕ drops it; a 413
  stays as a red chip; row ✕ is confirm-gated; error/stale replies handled.
  **Probe landmine:** the probe emulates touch, and on touch Enter is a
  newline — tap `#send`, never `keyboard.press('Enter')`.
- `tools/checkall.sh` picks all of these up. Push = deploy; finish with
  `python3 tools/shipcheck.py --wait`.

## A worked example: the vision skill (2026-09-04)

Austin tapped `vision`, typed, sent. The pointer line arrived under his text;
the session Read the file. Setup on a fresh box per the skill: `gh repo clone
clawdbotatg/vision` into `~/vision`, write `.env` from the skill's block
(the four `KEY=` lines — the skill indents them, strip the leading spaces),
`./vision.py probe` (the Hub answered with three cameras from the dev Mac).
Finding: the GitHub repo is **behind the skill** — no `cast` command, no
device cameras, cameras named `cam2`/`cam3` not `desk1`/`desk2`. The cast
server (iPad as a camera) was found by scanning the LAN for port 8790; it
answers `/status` (JSON, `devices.<name>.age`) and `/frame?name=<device>`
(JPEG, timestamp burned in) over self-signed https. Whichever box runs cast
has the newer `vision.py`; it hasn't been pushed.
