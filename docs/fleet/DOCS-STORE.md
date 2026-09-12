# Fleet doc store — the shared shelf (handoff doc)

Read this before touching the relay's `/docs/*` or the `fleet-docs` skill.
History with dates lives in [`../HISTORY.md`](../HISTORY.md) (2026-09-12).

## What it is, in one paragraph

A flat shelf of named files on the relay box (h.atg.link) that any agent on
any machine can write to and read from, over the web, with one token. Austin
tells a session on one computer "make a plan and put it in the doc store";
a session on another computer (or his laptop, or a box off the LAN) pulls it
down by name. No sync, no machines talking to each other, no git: one HTTP
store, four calls. The `fleet-docs` skill in the 📚 library carries the token
and the four curl lines, so a session with zero context can use it after one
tap.

## The four calls

All under `https://h.atg.link/docs/`, all take `?t=<DOCS_TOKEN>`:

| Call | Method | In | Out |
|---|---|---|---|
| `/docs/list` | GET | — | `{docs:[{name,size,mtime}]}` |
| `/docs/get?name=X` | GET | — | the raw bytes, content-type by extension (`.md` → text/markdown, unknown → octet-stream); 404 if missing |
| `/docs/put?name=X` | POST | raw body | `{ok,name,size,url}`; upsert |
| `/docs/rm?name=X` | POST | — | `{ok,removed}`; 404 if missing |

Names: `[A-Za-z0-9._-]`, first char alnum, ≤128 chars, flat (no `/`). Use
a suffix so the reader knows what it is (`plan-vision-cast.md`). Caps:
4 MB per doc (`FLEET_MAX_DOC_BYTES`), 2000 docs (`FLEET_MAX_DOCS`); nginx's
`client_max_body_size 26m` on `location /` is above that.

```bash
curl -sS --data-binary @plan.md "https://h.atg.link/docs/put?t=$T&name=plan.md"
curl -sS "https://h.atg.link/docs/list?t=$T"
curl -sS "https://h.atg.link/docs/get?t=$T&name=plan.md" -o plan.md
curl -sS -X POST "https://h.atg.link/docs/rm?t=$T&name=plan.md"
```

## Store (`fleet/relay.py`, the "fleet doc store" section)

- Dir: `fleet/.clawd-fleet.docs/` next to `relay.py` (`FLEET_DOCS_DIR` to
  move it), gitignored (`fleet/.clawd-fleet.docs*`). Writes go tmp +
  `os.replace`, so a half-written doc is never served.
- **Overwrite and remove both keep the old bytes** in
  `.clawd-fleet.docs/.trash/<name>-<epoch>[-n]` — a dot-dir, never listed.
  An admin on the box (`ssh zkllmapi`) restores with `mv`.
- **Its own token.** `FLEET_DOCS_TOKEN` env, else
  `fleet/.clawd-fleet.docs.token` (auto-generated, mode 600, on the first
  start without one). It is deliberately NOT the worker token: the docs
  token rides inside a library skill that lands in session transcripts and
  upload dirs on every machine; if it leaks, someone can read and write this
  shelf, not register a machine or push to a session. No token file and no
  env → `/docs/*` answers 403 and the boot banner says so.
- Constant-time compare (`_token_ok`), like every other relay token.

To rotate: `ssh zkllmapi`, write a new value into
`~/clawd-harness/fleet/.clawd-fleet.docs.token`, `sudo systemctl restart
clawd-fleet-relay`, republish the `fleet-docs` skill with the new token
(`skillput`), done. Nothing else holds it.

## The skill (`fleet-docs`, in the private 📚 library — NOT in this repo)

The skill is the product surface: it holds the token, so it lives only in
the library (gitignored on the relay box). Republish it from a private
working dir after any change to the calls; delete the dir after. The
template, `<DOCS_TOKEN>` to be filled from the relay box:

```markdown
---
name: fleet-docs
description: Shared doc store on h.atg.link — put a plan/notes/file where a session on ANY other machine can get it, or fetch what another machine left. Use when Austin says "put that in the doc store / shared docs / where the other machine can get it", "grab the plan from the store", "what's in the doc store".
---

# Fleet doc store

One shelf of named files on https://h.atg.link, reachable from every machine
(LAN or not). Any session can write and read it with this token. Don't echo
the token in replies.

    T=<DOCS_TOKEN>

    # list           → {"docs":[{"name","size","mtime"}]}
    curl -sS "https://h.atg.link/docs/list?t=$T"
    # put (upsert)   → {"ok":true,"name":...,"size":...}
    curl -sS --data-binary @FILE "https://h.atg.link/docs/put?t=$T&name=NAME"
    # get            → the raw file (404 if missing)
    curl -sS "https://h.atg.link/docs/get?t=$T&name=NAME" -o FILE
    # remove (trashed on the box, an admin can restore)
    curl -sS -X POST "https://h.atg.link/docs/rm?t=$T&name=NAME"

Names: letters, digits, `.`, `_`, `-`; no spaces or slashes; ≤128 chars; keep
the extension (`plan-foo.md`). 4 MB per doc.

## How to behave

- **Storing**: write the document to a local file first, then put it. Name
  it for the topic, not the machine (`plan-vision-cast.md`, not `plan.md`),
  so the shelf stays readable as it grows. Reply with the name and one line
  on what it holds — that's what Austin will tell the other machine.
- **Fetching**: if Austin names a doc, get it. If he says "the plan" and
  doesn't name it, list first and pick the obvious match; if two could fit,
  show the list and ask. Read the file, then do what he asked with it.
- **Updating**: put again under the same name. The old version is kept in
  the trash on the box, so overwriting is safe.
- **Cleaning up**: only remove when asked.
```

## Tests

- `fleet/test_docs_store.py` — real relay on a tmp store: auth (docs token
  only — the worker token does NOT open it), name fences, size cap,
  put/list/get round-trip with content-types, upsert → `.trash`, rm →
  `.trash`, 404s, `.trash` never listed. `tools/checkall.sh` picks it up.
- Live check after a deploy: the four curl lines above against h.atg.link
  with the token from the box.
