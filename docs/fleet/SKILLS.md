# Fleet skills — the private skill library

One stack of **user-written skill files**, stored on the relay box, shown as
one list on every machine and device via the harness **📚 picker**. Tap a
skill → its SKILL.md text is **pasted into the open session** as a message.
That's the whole product: Austin writes down how to do a thing once (drive the
3D printer, post to the Vestaboard, speak in the kitchen), and hands it to any
session anywhere with one tap.

**The library is deliberately decoupled from machines.** It never scans,
installs to, or deletes from `~/.claude/skills/` anywhere — a first build that
synced skills onto every box (2026-08-30, same day) was ripped out for exactly
that reason; a one-shot janitor in `worker.py` (`_skills_sync_cleanup`)
removes what it had installed. Skills reach a session only as pasted text.

## How it flows

```
skillput <dir>  ──POST──▶  relay box: fleet/.clawd-fleet.skills/<name>/
                            (gitignored store, worker-token-gated HTTP)
                                   │
        📚 picker: {skillsLib} ────┤  fleet mode: browser → relay socket
        one list, every device     │  direct mode: harness proxies over
                                   ▼            /skills/lib (fleet.env creds)
        tap a skill → SKILL.md body pasted into the open session
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
  🕘, visible in a session). Tap a row → `sendQuick(body)` pastes the file;
  ✕ → `confirm()` → `skillsRm`; every reply repaints the open modal.

## Writing a skill

The `add-skill` skill in the library is the canonical how-to. Short version: a
dir named for the skill holding a `SKILL.md` (`---\nname: …\ndescription:
…\n---` frontmatter, then instructions any session could follow), then
`skillput <dir>`. **Never put credentials in a skill** — the store is private
(gitignored + token-gated) but the text gets pasted into sessions; reference a
per-box env file (the todo pattern) instead. LAN IPs/hostnames are fine.

## Tests

- `fleet/test_skills_lib.py` — store + library end to end: auth, hostile-path
  fences, bodies over HTTP and WS, trash-on-remove, the sync janitor.
- `test_skills_frame.py` — the direct-mode harness proxy (real relay behind it).
- `tools/skillbookprobe.mjs` — the 📚 modal: fetch on open, tap pastes the
  body, ✕ is confirm-gated, error/stale replies handled. Real touch gestures.
