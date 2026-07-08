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
