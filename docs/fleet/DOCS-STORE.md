# Fleet document store

The private shelf at `https://h.atg.link/docs/` shares documents across authorized
fleet machines. Each machine has its own revocable credential. Sessions on one
machine share that identity; this is not isolation between processes using the
same OS account. By default all provisioned machines share the same shelf.
An authorized writer can still replace other writers' files on that shelf.
Use read-only permissions or filename prefixes when stronger separation is needed.

## Agent workflow

The canonical skill is [fleet-docs-SKILL.md](fleet-docs-SKILL.md). Publish that
credential-free text to the private library. `share/bin/fleet-docs` is installed
as `~/bin/fleet-docs`; it reads the mode-600 file
`~/.config/fleet-docs/credential.json` without printing its token or passing it
in command-line arguments. Downloads are saved as private files, never executed.

```
~/bin/fleet-docs list
~/bin/fleet-docs put plan-example.md /tmp/plan-example.md
~/bin/fleet-docs get plan-example.md /tmp/fetched-plan.md
~/bin/fleet-docs rm plan-example.md
```

## HTTP contract

All four endpoints require `Authorization: Bearer <credential>`. Query tokens,
including the former skill token, are rejected. Missing or invalid credentials
fail closed. POST requires exactly one valid Content-Length; chunked, incomplete,
negative and malformed bodies are rejected without changing the document.

| Endpoint | Method | Permission | Response |
| --- | --- | --- | --- |
| `/docs/list` | GET | read | Names, byte sizes, mtimes filtered by allowed prefix |
| `/docs/get?name=X` | GET | read | Raw bytes as a sandboxed attachment |
| `/docs/put?name=X` | POST | write | `{ok,name,size}`; previous version retained |
| `/docs/rm?name=X` | POST, empty body | delete | `{ok,removed}`; previous file retained |

Names begin with an ASCII letter/digit and contain only letters, digits, `.`, `_`,
`-`, up to 128 characters. No paths, dotfiles, trailing newlines, or symlinks.
All downloads use application/octet-stream, Content-Disposition: attachment,
a sandbox CSP, nosniff, no-referrer, same-origin resource policy, and no-store.

## The shared dictation word list (`stt-words.txt`)

One document on this shelf is written by the harness PAGE, not an agent: the
🎤 word list from ⚙️ settings. The page reaches it through `GET|POST /stt/words`
(gated by the passkey session `s=` like `/upload`, text/plain, ≤ 64 KB) — no
doc credential in a browser. `clawd-dictate` on the Mac and the phone keyboard
read the same file with their machine's credential (`fleet-docs get
stt-words.txt`). A word added on any surface reaches the others: the page pulls
when Deepgram creds arrive and when ⚙️ opens, the Mac tool every five minutes.
`fleet/test_stt_words.py`.

## Storage and abuse limits

`fleet/docs_store.py` implements the store. Directory mode is 700; new files and
credentials are 600. Relative directory descriptors and no-follow opens confine
reads/writes, including `.trash`. Hardlinked files are rejected too.
Overwrites write a new temporary file and preserve the prior bytes before an
atomic replacement; a failure does not first remove the current document.

| Environment setting | Default |
| --- | --- |
| FLEET_DOCS_DIR | fleet/.clawd-fleet.docs |
| FLEET_MAX_DOC_BYTES | 4 MiB |
| FLEET_MAX_DOCS | 2000 active documents |
| FLEET_DOCS_MAX_TOTAL_BYTES | 256 MiB including trash/temp and transient copies |
| FLEET_DOCS_MAX_ENTRIES | 4000 files including trash/temp and transient copies |
| FLEET_DOCS_RESERVE_BYTES | 256 MiB free filesystem space |
| FLEET_DOCS_CREDENTIALS_FILE | fleet/.clawd-fleet.docs.credentials.json |

Quota exhaustion returns 507; no historical files are automatically purged.
An administrator reviews retention and removes approved versions from `.trash`
over SSH to reclaim quota. Removals move data into trash without increasing its
byte count. Keep backups separately: same-disk trash is not a disaster backup.

The relay allows eight concurrent docs handlers, 2 requests/second per identity
with burst 30, and 20/second globally with burst 60. Excess receives 429 and
Retry-After. Authenticated uploads are bounded to 4 MiB and incomplete body reads
time out. nginx adds per-IP limits before request buffering/authentication:
5 requests/second with burst 20 and eight active connections per IP; globally
20 requests/second with burst 40 and 32 active connections; 4 MiB upload limit,
10-second body inactivity timeout and 20-second upstream timeouts. These bounds
limit abuse; they do not promise availability against distributed network floods.

Install `fleet/deploy/harness-docs-limits.conf` under `/etc/nginx/conf.d/` and
`harness-docs-location.conf` under `/etc/nginx/snippets/`. Include the latter only
in h.atg.link's HTTPS server block. It requires the existing `noquery` log format.
Always run `sudo nginx -t` before reloading.

## Credential administration

Run `fleet/docs_admin.py` on the relay host, serially (no concurrent registry
writers). It writes files atomically with mode 600. The registry stores SHA256
hashes, identities, operations and allowed filename prefixes, never raw tokens.
It is reloaded on every request; revocation needs no restart.

```
python3 fleet/docs_admin.py provision clawd-example --out /private/path/credential.json
python3 fleet/docs_admin.py provision reader --permissions read --prefix shared- --out /private/path/reader.json
python3 fleet/docs_admin.py revoke clawd-example
```

Transfer each output file over SSH to that machine's
`~/.config/fleet-docs/credential.json` in a mode-700 parent directory, with mode
600. Install `share/bin/fleet-docs` as `~/bin/fleet-docs` (755). Remove the staging
copy after verifying access. Never publish credentials in the library or git.
The legacy `.clawd-fleet.docs.token` and FLEET_DOCS_TOKEN are no longer consulted.
Provision clients and the registry before upgrading the relay and skill.

## Verification

`python3 fleet/test_docs_store.py` covers round trips, access gates, naming,
size limits and recoverable changes. `python3 fleet/test_docs_security.py` covers
quotas including history, failed replacement, symlinks/hardlinks, file permissions,
HTML isolation, scoped credentials, immediate revocation, malformed requests,
rate limits and disk reserve. Run the fleet tests when changing this surface.
