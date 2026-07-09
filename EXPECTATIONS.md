# EXPECTATIONS.md — the subscription-routing contract

Written 2026-07-08 (Claude + Austin), after the 🧠 rework. **This file is the
contract.** If reality diverges from anything below, that's a bug — point an
agent at this file, tell it what you saw, and the "when it breaks" section at
the bottom says what evidence to gather. Deep mechanics: `docs/fleet/SUB-ROUTING.md`.

## The three promises

### 1. Sign in once per (subscription × machine), and it sticks
One OAuth ceremony stores a refresh token in that machine's Keychain
(per-account `CLAUDE_CONFIG_DIR` under `~/.clawd-accounts/<name>`; `default` =
plain `~/.claude`). Claude Code renews its own access tokens indefinitely —
reboots, harness restarts, and `--resume` all reuse the stored login. You
should never re-do a ceremony **unless Anthropic invalidates the grant
server-side** (rare, but it happened to five logins at once before 2026-07-08).
The contract when that happens:
- the harness notices within one poll (~3 min), flips the login to
  `needs-login`, and **excludes it from routing** — nothing spawns onto a dead
  login, and no session silently burns one;
- the 🧠 page shows a red ⚠ `needs sign-in` chip on the affected plan card
  plus a one-tap `sign in again` button in the machine's section.
You should never *discover* a dead login by watching a session fail on it.

**2026-07-08 addendum — why logins kept dying, and why the next sign-in
should be the last (see "Root cause" below).** Before this date, promise 1
was structurally broken by the harness itself: idle logins died over and
over (the log shows `clawd` re-signed-in **12 times** on heart). That was not
Anthropic randomly revoking, and it was not the user's fault. It is fixed,
and the fix is verifiable in the log.

### 2. New work always lands on the pool with the most headroom
Every new session spawns under the ready login with the most headroom **on
that machine, at that instant** (fresh poll, not stale cache; stale = >3×TTL is
ignored). "Headroom" = what's left of the *most-constrained* usage window,
including model-scoped ones (e.g. the Fable weekly cap) — the binding window
is named right on the card (`headroom · 7d fable`). Running sessions move only
when it's worth it: the target must beat the current pool by
`SUB_HYSTERESIS` (20) points, with a switch debounce — no flapping.

### 3. With headroom on the machine, you are never stuck
**The guarantee:** if the machine a session runs on holds a working login for
any pool with headroom, work continues. When a session's pool drains
(≥ `SUB_EXHAUSTED` = 95% used, or its login breaks, or the usage endpoint
itself 429s — treated as fully used on purpose), the session is respawned
under the best pool with `--resume`, transcript linked across, same cid, same
viewers. This is not theoretical: `~/Library/Logs/clawd-harness.log` shows it
firing in production (`[handoff …] clawd → ef (plan drained; resuming under
the fresh one)`).

**The two honest caveats:**
- **Handoff is per-machine.** A session on heart can only switch between
  logins *heart* holds. A pool signed in only on leftclaw cannot rescue heart.
  Corollary: every machine should hold every subscription's login — the ⚠
  chips on the 🧠 page are the to-do list for that.
- **You may glimpse the limit banner once.** If a window dies mid-turn,
  Anthropic prints its limit message inside that claude and the turn ends
  early — the harness can't intercept text inside the child. On the next Stop
  (or the stuck-session sweep, within ~a minute) the session hops pools; it
  does NOT auto-retype the interrupted prompt, so worst case you say
  "continue" once. Preemptive autoswitch (promise 2) usually moves sessions
  *before* this point — given somewhere to move to.

So the invariant, phrased for pointing at later: **"I have a sub with
headroom signed into this machine, therefore I can keep working; at worst one
turn ends early and the session heals itself before my next message."**

## What the 🧠 page means (display contract)

- **Plans board (top, fleet mode):** ONE card per usage pool. A pool's
  identity is the **organization UUID from the OAuth token** (via the
  undocumented `/api/oauth/profile` endpoint) — *not* the email, because one
  email can hold seats in several orgs (proven in this fleet:
  `austingriffith` max 20x and `Ethereum Foundation` max 5x share
  austin.griffith@ethereum.org), and *not* `.claude.json`, because files go
  stale against the keychain (proven: head's "clawd" dir actually held
  austin's max login).
- Each card: org name, tier (`max 20x` / `pro` …), the **freshest machine's**
  windows, and one chip per machine+login (`🖥️ heart (ef) · 4% · 1s · 2 live`,
  freshest first). Within one pool, chips differing = polling lag, nothing
  else. Dead logins fold into their pool's card as ⚠ chips — a signed-out
  login is a footnote on a healthy plan, not a separate scary card.
- **Machine sections (below):** sorted freshest-check-first, offline last.
  Sign-in ceremonies, pinning, and removal live here. A failing usage poll
  says so on the card (`⚠ usage poll: … — showing the last good reading`)
  instead of silently going stale.
- Identity self-heals: every ~3-min poll re-fetches the token-bound identity,
  so a re-login under an old nickname corrects its own label within minutes.

## Changes that implemented this (2026-07-08, chronological)

| commit | what |
|---|---|
| `e6f89ba` | 🧠 page reworked: fleet plan summary, machines sorted by freshest check, binding-window labels, poll-error lines |
| `321e6f5` | one card per plan (then keyed by email), signed-out logins folded in as chips, 3-min polls, 429 → treated exhausted |
| `47a6798` | plans keyed by **org uuid** (email only as fallback); identity refreshed every poll; chips name the login |
| `d4cd882` | identity from the **OAuth profile endpoint** — token truth beats stale files; org name + tier displayed |
| `0f9f0e3` | org-less dead logins merge into their email's unique pool (no more twin signed-out cards); tier-first subtitles |
| `fc77c82` | docs: the relay box (h.atg.link UI) does NOT auto-pull — a UI deploy ends with a pull there |

Deploy contract: `git push` → worker machines self-update (~5 min; server.py
changes wait for idle sessions before the graceful restart) → the relay box
needs `ssh ubuntu@174.129.67.164 'cd ~/clawd-harness && git pull'` (now
covered by the `Bash(ssh:*)` allow rule) → **hard-reload the h.atg.link tab**
(an open tab never refetches by itself).

## Root cause v2 — THE REAL ONE (2026-07-09): Cloudflare, not revocation

> The v1 story below (rotation-discard) was **wrong**. It's kept because this
> file is a ledger, and because the user rightly held it against the contract:
> clawd died again overnight, hours after the "13th and FINAL" ceremony —
> a breach on this document's own terms (dead login, zero `[creds]` lines).
> The investigation that breach forced found the actual killer.

**The evidence trail (2026-07-09 morning):**
1. Zero `[creds]` lines in the log — the v1 write-back fix never engaged once.
2. Credential blobs show access tokens live **~8 h** and refresh tokens
   **~27 days** — so "expired beyond refresh" within 12 h was never expiry.
3. Every account froze ("checked N h ago") at exactly its 8-hour access-token
   expiry — every poller refresh attempt failed instantly.
4. The decisive test: POSTing the token endpoint from Python gets
   **HTTP 403 `error code: 1010`** — a **Cloudflare bot-block** at the edge.
   The same request via `curl` reaches Anthropic's real OAuth service.

**The actual mechanism of every "death":** access token expires (8 h after
claude last persisted one) → poller tries to refresh → **Cloudflare 403s
Python's TLS signature; the request never reaches Anthropic** → the harness
misread the failed refresh as "credentials refused" → login flagged
needs-login, routing excluded, scary red card. **The stored refresh grants
were still valid the whole time. Nothing was ever revoked. None of the ~13
re-sign-in ceremonies were actually necessary** — each one merely minted a
fresh 8-hour access token, so the login "worked" until it sat idle past 8 h
again. This also explains "five logins died at once" pre-2026-07-08: that
will have been the day the Cloudflare rule started matching Python.

**The fix (deployed 2026-07-09):**
- Refresh grants go out via **curl** (passes Cloudflare; token piped via
  stdin so it never shows in `ps`).
- **Infra failures are never death sentences:** only an HTTP 400/401 from
  Anthropic's own OAuth service marks a login needs-login. Cloudflare
  blocks / 429s / outages → keep the last snapshot, log
  `refresh blocked in transit — transient`, try again next poll.
- **Single-consumer rule:** the poller never refreshes an account that has
  live claude sessions (those processes hold the same grant; two consumers
  of one rotating grant can race and kill the token family). Such accounts
  are polled with the stored access token only and show "access token stale —
  a live claude session renews it" rather than a false death.
- Every credential event is **timestamped** in the log now (`[creds <dir>
  MM-DD HH:MM:SS] …`) — the v2 post-mortem was nearly impossible without.

**The falsifiable prediction that tests all of this:** the "signed out"
logins across the fleet (ef + austinmax on heart, clawd on leftclaw/head…)
should **resurrect on their own** within minutes of each harness restarting
onto this fix — their grants were never dead, so the first curl-refresh mints
a fresh access token, persists it, and the card flips back to live **without
any human ceremony**. If they do: case closed. If any stays dead with
`refresh REJECTED by the OAuth service` in the log, that one was genuinely
revoked and needs the one ceremony the contract always allowed.

## Root cause v1 (2026-07-08, SUPERSEDED — see v2 above): the rotation-discard theory

**The pattern:** every login that sat idle (clawd, austinmax, ~/.claude
default — on every machine) kept dying "revoked or expired beyond refresh",
while `ef` — the one account claude itself ran under all day — never died.
Twelve re-sign-ins of clawd on heart alone.

**The mechanism:** the usage poller, when an idle account's access token had
expired, used the stored **refresh token** to mint a new access token — and
then **threw the response away**. `server.py` even documented it proudly:
*"We never write tokens back — Claude Code owns and refreshes its own."*
That's fine only if refresh tokens are reusable. If Anthropic **rotates**
refresh grants on use (issue a new one, kill the consumed one), then every
harness refresh of an idle account destroyed the stored credential's future:
the next refresh attempt — ours or claude's — hits a dead grant → 401 →
"needs re-sign-in". The busy account survived because claude persists its own
rotations to the Keychain; idle accounts had only the poller touching them,
and the poller was the assassin.

**The fix (commit of this date):** the poller now writes refreshed tokens
back to the credential store, exactly like claude does — new access token,
new expiry, and the rotated refresh token when one is issued — atomically,
and only when the store still holds the grant we consumed (a concurrent
claude rotation wins). Every write logs:
`[creds <dir>] refreshed access token persisted — refresh token ROTATED and persisted`.

**What this buys — "keep them fresh":** the poller touches every ready
account every ~3 minutes. With write-back, that loop IS the keep-alive: an
idle login now gets its tokens renewed and persisted indefinitely, the same
as an active one. Signing in is planting a tree, not lighting a candle.

**Honesty about confidence:** the rotation hypothesis fits all the evidence
but was confirmed-by-design, not by a live test (mutating a real credential
store ad hoc was correctly refused in-session). The first
`refresh token ROTATED` line in `~/Library/Logs/clawd-harness.log` is the
confirmation; if rotation turns out NOT to happen, the write-back is harmless
and the true killer is still at large — reopen the hunt, starting from
`refreshTokenExpiresAt` in the credential blob (absolute expiry would need
the keep-alive to renew *before* that horizon, which rotation-persistence
does automatically).

**The renewed promise for "sign in as clawd on heart":** this next ceremony
is the last one, under exactly two outs — (a) Anthropic revokes the grant
server-side (outside anyone's control, and now genuinely rare since we
stopped doing it to ourselves), or (b) the log shows `WRITE FAILED` (Keychain
refused the write — surfaced loudly, not silently). Anything else that kills
a login after this date is a **breach of this contract**: bring this file
and the log.

## Sign-in ledger — what each ceremony bought, verified

### clawd on heart, 2026-07-09 morning (the 14th — `/login` in-session)
The 13th (below) died overnight: its access token expired ~03:00 and the
Cloudflare-blocked refresh was misread as revocation (root cause v2). The
user re-authed via `/login` inside a running session. **What's different
this time is the code, not the promise's volume:** the refresh path actually
works now (curl), an edge block can no longer be mistaken for a dead login,
and the single-consumer rule protects this very login (this session's claude
holds its grant, so the poller will never consume it). The proof to watch
for: `[creds …] refreshed access token persisted` lines with timestamps, and
— more telling — the OTHER dead logins resurrecting without ceremonies.

### clawd on heart, 2026-07-08 (the 13th time — "FINAL" claim RETRACTED)
Verified within minutes of the ceremony, against the live API and the live
router:
- login live: `clawd@buidlguidl.com's Organization` (own org
  `94f7f5f0…`, tier max 5x), 7d **76% left**, resets 2026-07-09 16:00 UTC;
- poller tracking it on the 3-min loop, token-bound identity broadcast;
- router flipped the moment it appeared: `best: clawd, active: clawd` —
  new sessions land there, and sessions on the drained 20x pool hand off at
  their next Stop.

**What to expect from this login, and why:**
- **You never sign clawd in on heart again.** The thing that killed it 12
  times (the poller consuming refresh grants and discarding the rotated
  replacement — see Root cause above) is fixed and was running BEFORE this
  ceremony, so this login has been under the keep-alive from its first
  minute.
- **The ongoing proof is in the log**, not in anyone's word: within hours
  (first access-token expiry) `~/Library/Logs/clawd-harness.log` on heart
  gets `[creds /Users/clawd/.clawd-accounts/clawd] refreshed access token
  persisted — refresh token ROTATED and persisted`. Every ~poll-cycle
  renewal after that is the same line. Silence + a dead login = breach;
  `WRITE FAILED` in the log = the Keychain refused us and the login will
  die at next refresh — either way the log names the culprit.
- **What ensures it:** the 3-min poll loop renews and PERSISTS every ready
  account's tokens indefinitely (idle or busy — idleness is what used to be
  fatal); writes are atomic, never clobber a concurrent claude rotation,
  and never silent.

## Known limits (not bugs)

- Both usage and profile endpoints are **undocumented**; the code degrades
  gracefully (keeps last snapshot, shows the poll error) but Anthropic can
  change them any day. If every card goes `usage unavailable` at once,
  suspect the endpoint, not the fleet.
- Handoff granularity is the turn: mid-turn exhaustion ends that turn (see
  caveat above).
- Five-hour windows anchor on first use per pool — heavy parallel use can
  drain several pools' 5h windows near-simultaneously; the weekly windows are
  the real budget.
- A signed-out login shows its **last cached** windows (or a synthetic
  `limited` row) — historical, not live.

## When it breaks, gather this (then point the agent here)

1. Screenshot the 🧠 page (h.atg.link, after a hard reload).
2. On the affected machine: `python3 tools/usage_probe.py <config_dir>` for
   the login in question (empty arg = `~/.claude`).
3. `grep -i "handoff\|account" ~/Library/Logs/clawd-harness.log | tail -30`
   — did the router try? what did it see?
4. Note which machine the stuck session was on and which pools that machine
   held at the time (promise 3 is per-machine — "there was headroom on
   another box" is the known limit, not a breach).
5. `curl -s https://h.atg.link/ | grep -c orgUuid` — is the served UI current?
