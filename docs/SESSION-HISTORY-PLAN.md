# Plan 1 — 📄 session history: close never forgets, reopen brings it back

Status: **planned 2026-09-03, not started.** Companion: `DOC-AND-CLOSE-PLAN.md`
(part 2, which depends on this one).

## The question that started it

"Can a session close itself from the harness?" — **today, no.** Exactly three
things close a session: the WS `close` frame (the ✕ in the UI, Ctrl+Shift+W),
the PM's `close` verb (same frame over the controller WS), and removing /
deleting a project. `SessionManager.close()` pops the session out of the
registry, SIGTERMs claude, and forgets it. Claude's transcript survives on
disk (`<config_dir>/projects/<slug>/<session_id>.jsonl`) but the harness has
dropped the only cid → session_id mapping, so nothing can find it again. That
is the actual "missing sessions" risk: not the process dying, the *mapping*
dying.

A child session does technically hold what it would need to call the harness
(the hook settings file it launches with carries the `/hook?t=TOKEN&cid=…`
URL) — but `/hook` is event-only, there is no command endpoint, and nothing is
sanctioned. Part 2 builds the sanctioned, gated path. Part 1 (this doc) makes
closing safe first, so that path has a net under it.

## What we're building

1. **Close = archive.** Every close appends the session's registry row (plus
   the things you'd want to read later: last answer, digest, why it closed)
   to an append-only archive. Nothing is lost by closing, ever.
2. **📄 history view.** A new top-level mode, `#/closed`, listing every
   archived session, newest first, with one filter box. Tap a row to see its
   summary; tap **⟳ reopen** to bring it back into the harness as a live
   `--resume` under its *original cid* (so pins, deep links and PM references
   keep working).
3. **Undo on close.** Closing a session shows a short toast "closed <title> ·
   undo". Undo is just reopen. The cheapest possible fix for a fat-fingered ✕.

Decisions already made (don't re-litigate): zero-turn sessions (no prompt, no
title) are not archived — they're noise, and "keeping things clean" is the
point. Sign-in ceremony sessions are never archived. Reopen resumes; it never
silently starts fresh (a missing transcript is an error you can see).

## Server (`server.py`)

### Archive file
- `CLOSED_FILE = HERE / ".clawd-harness.closed.jsonl"` — append-only JSONL.
  Add `.clawd-harness.closed*` to `.gitignore` (the existing globs cover
  `session*`/`token*`/`env*`, not this).
- Row = `s.to_registry()` **plus** `closed_at`, `closed_by`
  (`user` | `self` | `controller` | `project-removed`), `last_answer` (full
  500), `digest`, `blocked_on`, `project_name`, `transcript` (the path
  `_find_transcript()` resolves *at close time*, so reopen doesn't re-glob
  and survives an account dir moving).
- In-memory mirror `self.closed` (list, newest first) loaded at boot.
  `CLOSED_KEEP = 500`; when the file exceeds 2× that, rewrite it with the
  kept tail (atomic write, same pattern as `save_registry`).
- Reopening a cid removes its row (rewrite). It lands back in the archive the
  next time it closes, with fresh data.

### `close(cid, reason="user", _broadcast=True)`
- Before `s.kill()`: if `not s.ceremony and (s.prompt_count or s.title)`,
  `self._archive(s, reason)`.
- The three existing call sites pass a reason: WS `close` → `"user"`
  (the controller rides the same frame — the WS handler can tell them apart
  by `client.is_controller` if that flag exists, else the verb passes
  `reason:"controller"` in the frame); `remove_project`/`delete_project`
  paths → `"project-removed"`.
- **Verify respawn paths never route through `close()`** (handoff / onboarding
  heal use `clone_for_respawn` + `kill`/`shutdown`). A respawn must not leave
  an archive row. `test_closed_archive.py` asserts this.

### `reopen(cid, client=None)`
- Live already → reply `focus` and stop (no double spawn).
- Row missing → error `reopen: not in history`.
- Project gone (`pid not in self.projects`) → error `reopen: project <name>
  is no longer on this machine — add it back first`.
- **Factor the boot-restore per-row logic into `_restore_row(e) -> session`**
  and use it from both `boot restore` and `reopen`. Reopen *is* "restore this
  one registry row", including the whole resume gate (signed-out account →
  land on the plan with headroom, `_link_transcript` across; codex → resume
  under its own store). Do not copy those 100 lines.
- Transcript missing → error `reopen: transcript is gone (history cleared?)`.
  Do **not** start fresh silently. (v2 idea, not now: a "start fresh in this
  project with the old summary pasted" button.)
- Success: `self.sessions[cid] = s; s.start(); save_registry();
  broadcast_sessions()`; reply `{type:"focus", cid}` to the requester (the
  same frame `add_account` already uses — the UI dives into it).

### WS frames (update `docs/WS-PROTOCOL.md` in the same commit)
| frame | args | reply |
|---|---|---|
| `closedList` | `q?`, `limit?`, `id?` | `closedListResult {id, q, rows:[…], total, truncated}` **to the sender only**. Server-side substring filter over title/desc/tab/first_prompt/last_answer/project_name. Newest closed first. `limit` clamped to 200; `last_answer` trimmed to 280 in rows. |
| `reopen` | `cid` | `focus {cid}` on success; `error {error:"reopen: …"}` otherwise. |
| `close` | `cid`, `reason?` | unchanged wire shape; `reason` optional (controller sets it). |

Fleet: the worker forwards frames verbatim, so nothing changes relay-side.
The UI must send `closedList` to **every** machine and merge the results
tagged by machine (check how `accounts` frames are tagged per machine today
and do the same), and send `reopen` via `hsendTo(machine, …)`.

## UI (`index.html`)

- **📄 button** immediately right of 🕘 in `#quickchips` (static markup, so
  chips keep inserting before 🕘). Title: "History — every session you've
  closed. Find one, read its summary, reopen it." Click → `setView('closed')`
  → `#/closed`. Add `#/closed` to the hash router next to `#/pins`/`#/irons`,
  and to `docs/DEEPLINKS.md`.
- **View** (`#closedview`, same skeleton as the irons list):
  - one filter box at the top, debounced 150 ms → `closedList {q}`. Arrival
    focuses it; nothing else ever does (landmine 4).
  - rows: project emoji + name · title · "3d ago" · a small `closed_by` tag
    (user / self 📑 / PM / project-removed) · one-line desc.
  - tap a row → expands in place: digest, `last_answer` (this is the handoff
    summary once part 2 lands), tab; buttons **⟳ reopen** (primary) and
    **copy summary**.
  - **repaint law:** reconcile nodes by `closed-<cid>` id, never
    `innerHTML=''`. Results arrive on a debounce and on every close, and a
    finger may be mid-tap on a row.
  - empty state: "nothing closed yet — sessions you ✕ end up here".
  - fleet: rows carry `machine`; show the machine chip like the rail does.
- **Undo toast**: `closeSession()` remembers `{cid, title, machine}` and shows
  a 5 s toast "closed <title> · undo". Undo → `reopen`. The toast pattern
  already exists for errors; reuse it.
- **Landing**: the `focus` frame already drives navigation; after reopen the
  user lands in the session's tty like after a spawn.

## Controller (`controller/`) — three places together
- verbs: `closed(machine, q="")` (read, ungated) and `reopen(machine, cid)`
  (gated like `close`: `confirm`). `close` gains `reason:"controller"`.
- MCP descriptions for both.
- persona (`prompts/private.md`): one line — closed sessions are in history,
  reopen them instead of spawning a duplicate when the human refers to
  earlier work.
- `docs/CONTROLLER.md` verb table.

## Tests / probes / gate
- `test_closed_archive.py` — runs against a **copy** of `server.py` in an
  isolated dir (the registry trap). Cases: close archives a row; zero-turn
  and ceremony sessions don't; file bounded at 2×KEEP; reopen with a fake
  transcript → same cid, `resuming=True`, row removed; missing transcript →
  error; missing project → error; live cid → focus, no spawn; respawn leaves
  no row; `closedList` filter + limit clamp.
- `tools/closedprobe.mjs` — fake sessions + stubbed `hsend` (splashprobe
  pattern), emulated touch, real taps: 📄 opens `#/closed`; typing in the
  filter survives a `closedListResult` repaint mid-keystroke; tapping ⟳
  sends `reopen {cid}` (to the right machine in fleet mode); ✕ on a session
  shows the undo toast and undo sends `reopen`. Screenshot at the end —
  **look at it**.
- `tools/checkall.sh` picks both up automatically. Finish with
  `python3 tools/shipcheck.py --wait`.

## Docs
`docs/WS-PROTOCOL.md`, `docs/DEEPLINKS.md`, `docs/CONTROLLER.md`,
`docs/HISTORY.md` (dated entry), `CLAUDE.md` (add `#/closed` to the route
list in the index.html bullet; one landmine line: "close archives, reopen
resumes under the same cid — never start fresh silently"), `README.md` (📄).

## Order of work
1. Server: archive on close + `_restore_row` refactor + `reopen` + the two
   frames + `test_closed_archive.py`. Gate green.
2. UI: 📄 + `#/closed` view + undo toast + `closedprobe`. Gate green.
3. Fleet check on the phone via the relay (rows tagged, reopen routed).
4. Controller verbs + MCP + persona.
5. Docs, `checkall`, `shipcheck --wait`.

Rough size: server ~150 lines, UI ~250 lines, tests/probe ~200, docs ~60.
