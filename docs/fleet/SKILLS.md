# Fleet skills — the private skill library

One library of Claude Code skills, available to **every session on every fleet
machine**, for the automations that must never ride the public repo: LAN device
control (3D printer, Vestaboard, cameras, kitchen speaker), phone/house
integrations, anything with internal detail. The repo's `share/skills/` channel
still exists for secret-free skills (todo) — this is its private sibling.

## How it flows

```
skillput <dir>  ──POST──▶  relay box: fleet/.clawd-fleet.skills/<name>/
                             (gitignored store, worker-token-gated HTTP)
                                    │  every worker polls /skills/manifest
                                    ▼  every ~5 min (FLEET_SKILLS_SYNC_INTERVAL)
                          ~/.claude/skills/<name>/   on every machine
                                    │  SHARE_PATHS symlink (server.py)
                                    ▼
                          every account → every session, natively
```

- **Store** (`fleet/relay.py`): `.clawd-fleet.skills/` next to relay.py on the
  box, one subdir per skill (`SKILL.md` + helpers). Endpoints:
  `GET /skills/manifest`, `GET /skills/get?name=`, `POST /skills/put` — all
  gated by the **worker token** (machines are the trust domain; phones never
  need these). Names `[a-z0-9._-]`, relpaths fenced (no dotfiles/traversal,
  ≤64 files, ≤4 MB), whole-dir swap on publish so a half-upload never serves.
- **Sync** (`fleet/worker.py` `sync_skills_once`): pulls the manifest, installs
  changed skills into `~/.claude/skills/` (plus any account whose `skills/` is
  a real dir — the server.py opt-out case), removes library-deleted ones.
  State in `~/.clawd-fleet.skills.json` tracks what the sync installed **per
  file** — deletions only touch tracked files, so repo-kit and hand-placed
  skills are never harmed. Off-switch: `FLEET_SKILLS_SYNC=0`.
- **Publish** (`share/bin/skillput`, installed to `~/bin` on every box by the
  shared-kit sync): `skillput <skill-dir>` / `skillput list` / `skillput rm
  <name>`. Config from env or the checkout's `fleet/fleet.env`
  (`FLEET_RELAY` + `FLEET_WORKER_TOKEN`).
- **Picker** (`index.html`): the 📚 button at the right end of the quick-chip
  strip (right of 🕘, visible in a session). Opens a modal listing this
  machine's `~/.claude/skills` (`skillsList` WS frame → `skills` reply, so in
  fleet mode you see the machine you're driving). Tapping a skill
  **auto-sends** a pointer at its SKILL.md into the open session — a path
  pointer rather than a `/slash` so it works mid-turn, in sessions started
  before the skill synced, and under codex.

## Writing a skill

The `add-skill` skill in the library is the canonical how-to (ask any session
to "read the add-skill skill"). Short version: a dir named for the skill with
a `SKILL.md` (`---\nname: …\ndescription: …\n---` frontmatter, then the
instructions), then `skillput <dir>`. On every machine within ~5 min.

**Never put credentials in a skill body.** The store is private (gitignored +
token-gated), but skills fan out to every machine's disk — follow the todo
pattern: the skill references a per-box env file (`~/.clawd-<thing>.env`),
placed once by hand on the machines that need it, and instructs the session to
read it. LAN hostnames/IPs and device quirks are fine; keys are not.

## Tests

- `fleet/test_skills_sync.py` — relay store + worker sync end to end (publish,
  auth rejects, hostile paths, update, delete-only-tracked).
- `test_skills_frame.py` — `skills_meta` (the picker's server half).
- `tools/skillbookprobe.mjs` — the 📚 modal (open→fetch, rows, guarded
  outside a session, one tap = one pointer send).
