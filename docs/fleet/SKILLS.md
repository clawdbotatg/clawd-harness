# Fleet skills — the private skill library

One stack of **user-written skill files**, stored on the relay box, shown as
one list on every machine and device via the harness **📚 picker**. Tap a
skill → it **attaches to your next message** as a 📚 chip above the composer,
exactly like a dropped `.md` file; keep typing, and Enter sends your text plus
one instruction line per skill pointing claude at the uploaded SKILL.md (it
Reads it). That's the whole product: Austin writes down how to do a thing once
(drive the 3D printer, post to the Vestaboard, speak in the kitchen), and
hands it to any session anywhere with one tap — without the tap itself firing
anything off.

**The library is deliberately decoupled from machines.** It never scans,
installs to, or deletes from `~/.claude/skills/` anywhere — a first build that
synced skills onto every box (2026-08-30, same day) was ripped out for exactly
that reason; a one-shot janitor in `worker.py` (`_skills_sync_cleanup`)
removes what it had installed. Skills reach a session only as a file in that
machine's upload dir (`.clawd-harness-uploads/paste-…-<name>-SKILL.md`, the
same place a dropped `.md` lands) plus the one-line pointer in the message.

## How it flows

```
skillput <dir>  ──POST──▶  relay box: fleet/.clawd-fleet.skills/<name>/
                            (gitignored store, worker-token-gated HTTP)
                                   │
        📚 picker: {skillsLib} ────┤  fleet mode: browser → relay socket
        one list, every device     │  direct mode: harness proxies over
                                   ▼            /skills/lib (fleet.env creds)
        tap a skill → body → POST /upload (as <name>-SKILL.md) → 📚 chip
        Enter → "your text⏎Use the "<name>" skill: read <path> and follow it…"
        ✕ (confirm) → {skillsRm} → store dir moved to .trash/ everywhere-at-once
```

- **Store** (`fleet/relay.py`): `.clawd-fleet.skills/` next to relay.py, one
  subdir per skill. HTTP (worker token): `GET /skills/manifest`,
  `GET /skills/get?name=`, `GET /skills/lib` (bodies included),
  `POST /skills/put` (publish, or `{delete:true}`). Names `[a-z0-9._-]`,
  relpaths fenced, ≤64 files / ≤4 MB, whole-dir swap on publish. **Remove =
  trash**: the dir moves to `.clawd-fleet.skills/.trash/<name>-<epoch>/` (a
  dot-dir, never listed) so a fat-fingered ✕ is admin-recoverable.
- **Picker frames**: fleet mode sends `{type:"skillsLib"}` / `{type:"skillsRm",
  name}` straight over the authed mobile→relay socket; direct mode sends the
  same frames to the harness, which proxies them (`serve_skills_lib`, config
  from env / the checkout's `fleet/fleet.env`). Both paths reply
  `{type:"skillsLib", skills:[{name, description, body}], error?}`.
- **Publish** (`share/bin/skillput`, in `~/bin` on every box via the shared-kit
  sync): `skillput <skill-dir>` / `skillput list` / `skillput rm <name>`.
- **UI** (`index.html`): 📚 at the right end of the quick-chip strip (right of
  🕘, visible in a session). Tap a row → `attachSkill(s)`: the body the reply
  already carries becomes a `File` and rides `uploadFile` with
  `{glyph:'📚', label, fold}` — the same pipeline, hold-on-Enter, and red
  error chip a dropped `.md` gets (fleet: bridged to the current machine; no
  protocol change). `composeSend` then folds the chip's one-line instruction
  (`skillInvocation`) on its own line after the text. One chip per skill;
  the box refocuses so you keep typing; empty box + Enter is the old one-tap
  flow. Chips don't survive a reload (same as any attachment) — re-pick.
  Row ✕ → `confirm()` → `skillsRm`; every reply repaints the open modal.
  Why a pointer, not the body pasted under your text: one short line of
  context per pick, a readable transcript, and one attachment behavior to
  understand (a picked skill *is* a dropped `.md`). Writing it into the
  project as an installed skill stays off the table (see above).

## Writing a skill

The `add-skill` skill in the library is the canonical how-to. Short version: a
dir named for the skill holding a `SKILL.md` (`---\nname: …\ndescription:
…\n---` frontmatter, then instructions any session could follow), then
`skillput <dir>`. **Credentials belong in skills** (2026-09-04, audited): the
store is a gitignored dir on the relay box, reachable only with the worker
token (constant-time compare) or over a passkey-authed phone socket (every
non-ping frame re-checks auth), over TLS, never in git. A used skill lands as
a gitignored file in that one machine's upload dir and in that session's
transcript once claude Reads it — the same footprint as any secret a session
reads, and nothing else: the 🕘 sent history and the harness prompts log hold
only the pointer line. Keep each secret on its own line (one-line rotation),
tell the reader not to echo it, and delete the local working dir after
publishing. The earlier "never" rule dated from the paste-into-PTY era.

## Tests

- `fleet/test_skills_lib.py` — store + library end to end: auth, hostile-path
  fences, bodies over HTTP and WS, trash-on-remove, the sync janitor.
- `test_skills_frame.py` — the direct-mode harness proxy (real relay behind it).
- `tools/skillbookprobe.mjs` — the 📚 modal: fetch on open, a tap uploads the
  body (stubbed `/upload`) and attaches a 📚 chip with nothing sent, a second
  tap dedupes, typing + 📤 sends ONE message (text ⏎ pointer line), the chip
  ✕ drops it, a 413 stays as a red chip, row ✕ is confirm-gated, error/stale
  replies handled. Real touch gestures and keystrokes.
