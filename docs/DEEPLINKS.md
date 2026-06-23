# Deep-linking into projects & sessions

How to construct a URL (or notification) that opens the harness UI **straight on
a specific session** — including the multi-machine fleet case. Written for anyone
(human or agent) building something that needs to link into the app.

Nav state lives entirely in the **URL hash** (the `?t=` token, if any, stays in
the query). So a hash is a shareable, reload-survivable pointer to a place in the
app. The page parses it on boot (`parseHash()` in `index.html`) and resolves it
against live server data (`resolvePendingNav()` / `resolvePendingNavFleet()`).

There are **two modes**, and they use **different hash grammars** — get this
wrong and the link silently falls back to a list view.

---

## Direct mode (talking to one harness — `127.0.0.1:8787`)

Here a project is identified by its **local pid**.

| Target | Hash |
|---|---|
| Projects list | `#/` |
| One project's sessions | `#/p/<pid>` |
| A session (transcript) | `#/p/<pid>/s/<cid>` |
| A session (terminal) | `#/p/<pid>/s/<cid>/tty` |

`<pid>` = the project id (the self-project is `self`). `<cid>` = the stable
console session id (NOT claude's rotating `session_id`).

---

## Fleet mode (through the relay — `h.atg.link`)

The fleet unifies the *same project across machines* under one **projectKey**, so
the hash keys on that, not a per-machine pid. Two forms:

| Target | Hash |
|---|---|
| A session, machine inferred | `#/p/<projectKey>/s/<cid>` |
| A session on a **specific machine** | `#/m/<machine>/p/<projectKey>/s/<cid>` |
| …its terminal | `#/m/<machine>/p/<projectKey>/s/<cid>/tty` |

### ⚠️ Always include the machine for a session deep-link

A project (e.g. `clawd-harness`) often exists on **several machines**. Without the
`m/<machine>` prefix the router can't tell which host holds the session and falls
back to the project's **default machine** — landing you on the *wrong machine's
session list*. The `#/m/<machine>/…` prefix makes it unambiguous; the resolver
honors the explicit machine first. **For any session link in fleet mode, include
the machine.** (The prefix is optional in the grammar only for backward compat.)

### projectKey — how to compute it

`projectKey = normRepo(repoUrl)  ||  "name:" + name`

`normRepo` canonicalizes the git remote so the same repo unifies across machines:

```
strip "git@host:" → "host/"      (git@github.com:o/r → github.com/o/r)
strip scheme       (https:// etc.)
drop trailing ".git"
drop trailing "/"
lowercase
```

So `https://github.com/clawdbotatg/clawd-harness.git` → `github.com/clawdbotatg/clawd-harness`.
A project with no remote falls back to `name:<project name>`.

**URL-encode the projectKey** when putting it in the hash — it contains slashes:

```
github.com/clawdbotatg/clawd-harness
→ github.com%2Fclawdbotatg%2Fclawd-harness
```

Canonical implementations (keep these two in sync if you touch either):
- JS: `normRepo()` / `projectKey()` in `index.html`
- Python: `Worker._norm_repo()` / `Worker._project_key()` in `fleet/worker.py`

---

## Recipe: build a fleet session deep-link

Given `machine`, the project's `repoUrl` (or `name`), and the session `cid`:

```js
const key = normRepo(repoUrl) || ("name:" + name);
const url = `/#/m/${encodeURIComponent(machine)}/p/${encodeURIComponent(key)}/s/${cid}`;
// → /#/m/clawd-head/p/github.com%2Fclawdbotatg%2Fclawd-harness/s/<cid>
```

Python (e.g. building a notification on a worker):

```python
from urllib.parse import quote
key = Worker._norm_repo(repo_url) or ("name:" + name)
url = f"/#/m/{quote(machine, safe='')}/p/{quote(key, safe='')}/s/{cid}"
```

Open it by setting `location.href`/`location.hash`, or from a service worker
`clients.openWindow(url)` / `client.navigate(url)`.

---

## How push notifications use this

The notification path is the live example: the worker watches its harness for
"needs you" hooks, then builds exactly this deep-link and sends it (encrypted) in
the push payload; the service worker navigates to it on tap.

- `fleet/worker.py` → `_push_payload()` builds `/#/m/<machine>/p/<key>/s/<cid>`
  (caches pid→name/repoUrl from the roster stream to compute the key).
- `sw.js` → `notificationclick` reads `notification.data.url` and focuses +
  `navigate()`s an open window there, or `openWindow()`s it.

See `docs/fleet/` and the fleet-notifications memory for the full push design.

---

## How resolution works (so you can debug a link)

1. `parseHash()` → `{ machine, key, cid, view }`. An unknown/empty hash → projects.
2. `resolvePendingNav()` runs on boot **and is re-driven on every projects/sessions
   frame + roster change** — it waits (leaving `pendingNav` set) until the data the
   URL names arrives, so a link works even before the server snapshots land.
3. Fleet (`resolvePendingNavFleet`): find the project group by `key` → pick the
   machine (**explicit `machine` wins**, else the one already holding the cid, else
   the default) → `attachMachine()` (loads that machine's sessions) → find the
   `cid` → `subscribe()` + show the view.

**Gotchas that make a link land on a list instead of the session:**
- projectKey doesn't match (e.g. forgot to URL-encode, or `normRepo` drift) → no
  group found → projects list.
- Missing `m/<machine>` in fleet mode → default machine → wrong session list.
- Stale page: an installed PWA caches the old `index.html`; if the grammar
  changed, **force-quit + reopen** before testing (it won't auto-reload).

A reload (or shared link) lands back on the same place because `syncUrl()` writes
the current location — including the `m/<machine>` prefix in fleet mode — back to
the hash as you navigate.
