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

### 2. New work always lands on the pool whose weekly window resets soonest
*(Policy changed 2026-07-09 at Austin's direction — it was "most headroom"
before. "With room" redefined 2026-07-11 evening after the session-wall
incident: the eligibility bar is now **cool** — < `SUB_HOT` = 90% of the
most-constrained window, which in practice is the fast-burning **5h session
window** — not < 95. The number is Austin's: spend the soonest-resetting
pool down to ~5–10% left, then hop. See "the session wall" below.)* Among
**cool** pools,
every new session spawns under the one whose **weekly (7d) window resets soonest**, on
that machine, at that instant (fresh poll, not stale cache; stale = >3×TTL is
ignored). Rationale, in Austin's words: a 50% pool and a 60% pool are *both
eligible* — weekly headroom is use-it-or-lose-it, so **drain the one that
resets soonest first**; picking by raw headroom spreads load evenly and
forfeits capacity at every reset. Once a pool's week resets, its clock jumps
+7 days and it goes to the back of the queue; once it crosses 95% it drops
out of eligibility and the next-soonest takes over. Headroom (the
most-constrained window, incl. model-scoped ones like `7d fable`) is now only
the **tie-break**, and the fallback when a pool's reset time is unknown.
Anti-flap: reset order is stable between polls, so a reset-driven switch
needs only the debounce; a headroom-driven fallback switch still needs
`SUB_HYSTERESIS` (20) points too.

**Expected visible behavior (not a bug):** all machines may pin the SAME
pool and drain it hard while other pools sit half-empty — that's the policy
working. The 🧠 page's "router →" line says which; the log's switch line says
why (`weekly resets Nh sooner — spend it before it's forfeited`).

### 3. With headroom on the machine, you are never stuck
**The guarantee:** if the machine a session runs on holds a working login for
any pool with headroom, work continues. When a session's pool drains
(≥ `SUB_EXHAUSTED` = 95% used, or its login breaks, or the usage endpoint
itself 429s — treated as fully used on purpose), the session is respawned
under the best pool with `--resume`, transcript linked across, same cid, same
viewers. This is not theoretical: `~/Library/Logs/clawd-harness.log` shows it
firing in production (`[handoff …] clawd → ef (plan drained; resuming under
the fresh one)`).

**Running sessions follow promise 2 too (added 2026-07-09 evening, at
Austin's direction).** Handoff used to be drain-rescue only: a session
spawned on yesterday's best pool would sit there for days while the
soonest-resetting pool forfeited capacity ("I have a session burning slop
but 100% it should be burning austingriffith — it resets the soonest").
Now the poller sweep also **rebalances**: an IDLE session on a healthy pool
moves — same seamless `--resume` handoff, same cid — whenever the router's
best pool's weekly window resets ≥ 6 h sooner (`SUB_REBALANCE_MARGIN`;
`SUB_REBALANCE=0` turns it off). Guard rails: never mid-turn (idle only),
never between logins of the SAME pool (one org, several config dirs — moving
buys nothing), only when both reset clocks are actually known (a blind/stale
poll is not a routing signal), and the per-session `HANDOFF_COOLDOWN` (10 min)
caps churn. Log signature: `[handoff …] slop → sub2 (rebalance: weekly resets
59h sooner — spend it before it's forfeited)`.

**Never see a rate limit (added 2026-07-11 evening, at Austin's direction —
"what I really really want is to never see a rate limit").** Four layers,
outermost first:
1. **Routing watches headroom, not banners** (the primary mechanism, per
   Austin: "look at how much is left; at 5–10% left switch to the next one
   with headroom that resets soonest"): a pool ≥ `SUB_HOT` = 90% on its
   most-constrained window (incl. the 5h session window) gets no new work
   while any cooler pool exists — so reset-soonest stops piling every
   session onto one pool until it walls.
2. **Preemptive evacuation:** the sweep moves an idle session off a hot pool
   to a cool one (`pool N% hot — evacuating before the limit wall`), and the
   on-Stop check (live endpoint read) does the same the moment a turn
   finishes — before the wall, not after.
3. **The banner itself is a tripwire:** the harness watches each session's
   raw PTY stream for the CLI's limit banner ("You've hit your session
   limit…" / the "Stop and wait for limit to reset" menu). On sight it
   confirms against the live usage endpoint (so a session merely *quoting*
   the banner — like this very file — is a no-op on a cool pool) and hands
   off within seconds: an eaten prompt is **redelivered**, and a turn cut
   mid-flight gets an automatic **"continue"** (`LIMIT_CONTINUE=0` opts out).
   *Honesty note (2026-07-12): this scan has not yet caught a wall in
   production — the 03:2x incident slipped past it. Layer 4 exists because
   of that, and logs the PTY evidence needed to fix the scan if it keeps
   missing.*
4. **The send watchdog (added 2026-07-12, ~3:20am, after a live miss):**
   every message the harness delivers must produce a `UserPromptSubmit`
   hook within ~2 s — but a hard-walled CLI answers with its limit line
   and fires **no hook at all** (nothing sets `busy`, so even the stuck
   sweep waits its full 10 min). So `send_message` arms a watchdog: total
   hook-silence `SEND_WATCHDOG` (10 s) after a delivery IS the bounce
   signal — no hook dependency, no terminal parsing. It logs the
   ANSI-stripped PTY tail (`send got NO hook in 10s on <acct> … pty tail:`)
   and feeds the same endpoint-confirmed rescue: dead pool → handoff +
   **redeliver the message**; healthy pool → no-op (a wedged-but-unwalled
   CLI is never respawned blind — that's the frozen-tty runbook's case).

**The two honest caveats:**
- **Handoff is per-machine.** A session on heart can only switch between
  logins *heart* holds. A pool signed in only on leftclaw cannot rescue heart.
  Corollary: every machine should hold every subscription's login — the ⚠
  chips on the 🧠 page are the to-do list for that.
- **You may glimpse the limit banner for a few seconds.** If a window dies
  mid-turn, Anthropic prints its limit message inside that claude and the
  turn ends early — the harness can't intercept text inside the child. The
  heal comes from whichever fires first:
  1. the **PTY tripwire** (layer 3 above) — seconds, auto-redeliver /
     auto-continue included (not yet observed firing in production — see
     the honesty note above);
  2. the next **Stop** hook → `maybe_handoff` moves it immediately;
  3. **your next message** — a prompt that lands on a hard-dead plan (≥100%
     used / login refused) bounces off the CLI's limit line **with no hook
     of any kind** (not even `UserPromptSubmit` — proven live 03:2x), so
     the **send watchdog** (layer 4) catches the silence at ~10 s, confirms
     against the endpoint, hands off, and **redelivers your bounced
     message** (~15–25 s total; log: `send got NO hook in 10s …` then
     `prompt bounced off dead plan … rescuing now and redelivering`);
  4. the stuck-session sweep — a busy-but-hook-silent session on a dead
     plan is reclaimed after `BUSY_STUCK` (10 min) + poll lag. Before
     2026-07-11 evening this backstop was the COMMON path (627 s and 881 s
     stucks observed in production); it is now the rare last resort.
  You should never have to *act* on a banner, only occasionally see one
  flash. When every pool on the machine is genuinely hot there is nowhere
  to hop — that's real exhaustion, not a bug.

So the invariant, phrased for pointing at later: **"I have a sub with
headroom signed into this machine, therefore I can keep working; a rate
limit may flash past, but the session heals and resumes the work by itself —
I never type around a limit."**

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
| `d116eb2`+`1fe0048` | identity headlines every card (nicknames demoted to small print, then dropped from display); machine panel groups by pool |

**2026-07-09** (the day the real killer was found — and the day GOAL.md was written):

| commit | what |
|---|---|
| `479b417` | **root cause v2**: Cloudflare 1010 was killing logins — refresh via curl; infra errors can never mark a login dead; single-consumer rule; timestamped creds log |
| `f159900` | GOAL.md — the two non-negotiables in the user's words |
| `d5a5152` | add-sub is one button — folder labels auto-picked server-side |
| `faece99` | the 429 death-spiral: never poll with known-expired tokens; honor Retry-After; 'limited' placeholders explain themselves |
| `52432b2` | back off on token-endpoint 429s too (the refresh retry was its own hammer) |
| `c32e2e3` | **routing policy change (Austin):** spend the pool whose weekly window resets soonest — headroom is only the tie-break (`_route_key`; promise 2 rewritten) |
| `26a8eea` | **rebalance (Austin):** promise 2 applied to RUNNING sessions — the sweep hands an idle session off a healthy pool to the reset-soonest one (≥6 h sooner, cross-pool only, both clocks known); before this a long-lived session pinned its spawn-day choice until it drained |

**2026-07-11** (the day v2's fix turned out to have never worked):

| commit | what |
|---|---|
| `548bcd3` | fleet: HarnessLink fd leak — 229 zombie links hit launchd's 256-fd limit (Errno 24), harness down; also the hour every account spuriously flipped "credentials refused" (unreadable Keychain ≠ dead login — see v3's residual) |
| `07c1edb` | **root cause v3**: the token endpoint 429s curl's DEFAULT User-Agent on every request — send `claude-cli/<version> (external, cli)` (`_claude_ua()`); not one background refresh had succeeded since v2 shipped |
| (next) | **v3's residual closed:** an unreadable credential store is transient, never a sign-out — `_read_oauth_creds_ex` distinguishes "keychain answered absent" (rc 44 + no file → AUTH_FAIL allowed) from "couldn't ask" (Errno 24, locked, timeout → keep last snapshot); ends the false mass "credentials refused" |
| (next) | **bounced-prompt rescue** (the zk-llm-research incident, same evening): a prompt landing on a hard-dead plan used to bounce off the CLI's limit line — no Stop, so no handoff, and the prompt hook re-armed the 10-min `BUSY_STUCK` clock (each retry delayed the rescue further). Now `UserPromptSubmit` on a ≥95% plan spawns `rescue_bounced_prompt`: settle 3 s (a real turn shows hooks and is left alone), confirm ≥100%/AUTH_FAIL against the live endpoint, hand off, wait for the fresh claude's SessionStart, **redeliver the bounced prompt** |
| (next) | **the session wall / never-see-a-rate-limit** (same evening, ~8pm): reset-soonest concentrated SEVEN sessions on the EF max-**5x** pool (through two dirs — `ef` + `sub3` are the SAME org, as `austinmax`/`clawd`/`sub2` are all the austingriffith org: 7 logins ≈ 4 real pools), blew its 5h **session window**, and every session hit the limit banner at once — one sat 627 s on the "Stop and wait" menu before the sweep reclaimed it. Fix: routing bar moves from 95 to `SUB_HOT` (90 — Austin: hop at 5–10% left; shipped at 80, retuned the same night) in `_route_key` (reset-soonest picks among COOL pools; a hot pool gets no new work while a cooler one exists); the sweep **evacuates** idle sessions off hot pools; on-Stop handoff moves at ≥80 (to a cool target) not just ≥95; and `_scan_for_limit` watches each PTY for the CLI's limit banner → `rescue_limit_wall` confirms against the endpoint and hands off in seconds, redelivering an eaten prompt or auto-sending "continue" for a turn cut mid-flight (`LIMIT_CONTINUE`) |

**2026-07-12** (~3:20am — the wall that slipped past every new defense):

| commit | what |
|---|---|
| (next) | **the send watchdog**: a live wall on heart (c7bba11c, clawd pool `limited` 100%) beat ALL of the above — the user's send bounced off the CLI's limit reply, which fires **no hook at all**, so the `UserPromptSubmit`-triggered rescue never ran, nothing set `busy`, `_scan_for_limit` matched nothing in the PTY, and the session sat **881 s** until the `BUSY_STUCK` sweep. Fix: `send_message` itself arms a watchdog — total hook-silence `SEND_WATCHDOG` (10 s) after a delivery the harness made IS the bounce signal (no hook needed, no terminal parsing), logs the ANSI-stripped PTY tail (evidence for why the banner scan missed), and feeds the same endpoint-confirmed `rescue_bounced_prompt` (healthy pool = no-op, so a wedged-but-unwalled CLI is never respawned blind). The rescue's `busy` gate is dropped — `hook_count` is the authoritative progress signal |

**2026-07-15** (the login-screen ambush — root cause v3's rule had two more unguarded gates):

| commit | what |
|---|---|
| (this) | **the spawn/resume ambush gates**: a fresh spawn on leftclaw (and later a spawn during a routine sub switch) opened onto the full OAuth login screen, then self-healed with no ceremony — the signature of a TRANSIENT verdict, not revocation. Cause: `_has_creds` collapsed `_read_oauth_creds_ex`'s "couldn't read the store" (locked keychain / fd exhaustion / timeout) into "absent", so the **create_session gate** and the **restart resume gate** — two paths v3's fix never reached — marked every account broken and spawned with no `CLAUDE_CONFIG_DIR` login. Fix: `_creds_state()` tri-state (`present`/`absent`/`unknown`); `unknown` spawns/resumes under the recorded account anyway (the claude child reads the keychain itself and usually succeeds where our read failed) and NEVER marks a login broken. `absent` still requires the store to have positively answered empty (keychain rc 44 + no file). Rule restated: **only Anthropic's OAuth service saying 400/401, or a store that answers "empty", may look like a sign-out — a failed READ is always transient** |

**2026-07-16** (the theme-picker ambush — a fresh spawn opens onto Claude's
first-run onboarding screen):

| commit | what |
|---|---|
| (this) | **onboarding-state ambush**: sessions spawned onto healthy, signed-in dirs (the 'AI DJ Prototype' / 'Audit Toolchain Scope' sessions, then a slop-computer-frontpage spawn) opened onto the full first-run onboarding (theme picker). NOT a credential problem — the login was valid the whole time. Cause: a sign-in ceremony closed after the OAuth step but before the theme question leaves the dir's `.claude.json` with a login and no `hasCompletedOnboarding`; resumed sessions skip onboarding, so the latent state hides for days until the first FRESH spawn routed there paints the picker (heart's austinmax dir carried it since ≥07-13). No credential gate can catch it — only claude reads that flag. Fix, two layers: **(1) spawn-path seed** — every spawn/resume/handoff runs `_ensure_onboarded`, which seeds `hasCompletedOnboarding: true` into any dir that already holds a login (theme unset = default rendering, same as our known-good dirs; a dir with NO login — a real ceremony — is left strictly alone); **(2) PTY tripwire** — `_scan_for_onboarding` watches each launch's first `ONBOARD_SCAN_WINDOW` (180 s) for the picker's needle text and `rescue_onboarding` seeds + respawns the SAME session under the SAME account past the screen, redelivering any prompt the picker ate (no hooks fire pre-onboarding — same silent-bounce signature as the limit wall). Capped at 2 respawns/cid; quoting the picker text later never matches (window closed). **Seeing the theme picker in any non-ceremony session is a breach — point here.** Log signatures: `half-finished onboarding completed`, `onboarding/theme screen in the PTY`, `onboarding ambush on <acct> — flag seeded; respawning` |

**End-state verified 2026-07-09 afternoon:** all four pools (austingriffith
20x · Ethereum Foundation 5x · clawd 20x · slop 5x) live with real numbers
on head, leftclaw, and heart simultaneously — chips identical across
machines, all three routers independently picking the same best pool. The
one still-unexercised path: the keychain write-back's first live run comes
at the first natural refresh (~8 h after the day's sign-ins); it either logs
`persisted` (case closed) or `WRITE FAILED` (named repair).

Deploy contract: `git push` → worker machines self-update (~5 min; server.py
changes wait for idle sessions before the graceful restart) → the relay box
needs `ssh ubuntu@174.129.67.164 'cd ~/clawd-harness && git pull'` (now
covered by the `Bash(ssh:*)` allow rule) → **hard-reload the h.atg.link tab**
(an open tab never refetches by itself).

## Root cause v3 (2026-07-11): the 429 wall — v2's curl never worked once

> v2 correctly diagnosed the killer (edge blocks misread as revocation) and
> correctly stopped marking logins dead. But its replacement refresh path was
> stillborn: **from the moment the curl switch shipped (07-09 08:53) to
> 07-11, every one of 1,706 background refresh attempts returned HTTP 429**
> — curl clears Cloudflare's TLS check, then Anthropic's app rate-limits
> curl's *default User-Agent* with a blanket `rate_limit_error` (JSON body,
> no Retry-After, permanent). The "transient — next attempt in 10 min" log
> line was honest about each attempt and wrong about the pattern: a wall of
> those lines is not decay, it's a client-identity block.
>
> **The symptom was ROUTING, not sign-ins** — which is why nobody saw it for
> two days. No login died (v2's promise held). Instead: 6 of 7 accounts'
> usage snapshots aged 15–50 h → `_best_account`'s 3×TTL freshness filter
> quietly shrank the candidate set to the ONE account a live claude session
> was keeping fresh (sub2) → every new session "won" the austingriffith pool
> — the one that resets LAST. Promise 2 inverted itself while every card
> looked plausible. Austin caught it by feel ("you should use the
> subscription that renews the soonest first!!!!").
>
> **The fix (`07c1edb`):** `_refresh_grant` sends
> `User-Agent: claude-cli/<version> (external, cli)` (version read from the
> real binary, pinned fallback). Verified on the same grant, same minute:
> curl's default UA → 429, claude-cli UA → 200.
>
> **Debugging rule this buys:** when routing picks a weird pool, check
> `checkedAt` ages in `.clawd-harness.sessions.json` FIRST. Stale usage
> doesn't crash routing — it silently collapses the candidate set before
> the reset-soonest comparison ever runs. Fresh data + wrong pick = policy
> bug; stale data = poller/refresh bug, and the policy never got a vote.
>
> **Residual (same day, from the fd-exhaustion outage) — FIXED same day:**
> when the Keychain read itself failed (`security` couldn't spawn — Errno
> 24), `_fetch_usage` saw "no tokens" and returned AUTH_FAIL — all seven
> accounts flipped "credentials refused", one per poll, 04:08–05:45. An
> infra failure marked as death, against v2's own rule, through a second
> unguarded path (v2 only guarded the *refresh* leg). Worse, the
> `refused_sig` gate then locked the false verdict in: the healthy,
> *unchanged* creds read as "same refused login still there", so only the
> two accounts whose live claude sessions rotated their tokens (sig
> changed) self-healed — the other five stayed excluded until the 13:28
> restart cleared the non-persisted `broken` flags. The fix:
> `_read_oauth_creds_ex` returns a `definitive` verdict alongside the blob
> — only "the keychain ANSWERED absent (rc 44) and no credentials file
> exists" may map to AUTH_FAIL; an unreadable store logs `credential store
> unreadable — transient` and keeps the last snapshot. Rule of thumb
> stands: a mass simultaneous "credentials refused" is a machine-health
> signal, not seven dead logins.

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

## The 2026-07-09 confidence statement — check me against this tomorrow

Written the evening all four pools went live on heart (austingriffith 20x ·
Ethereum Foundation 5x · clawd team 20x · slop 5x). **Claim: ~90% confident
none of these logins ever asks for a re-auth again**, and specifically the
overnight-death pattern is structurally dead — the ONLY code path that can
mark a login "signed out" now requires Anthropic's own OAuth service to
answer 400/401 `invalid_grant`. Edge blocks / 429s / outages degrade to a
stale-usage note on the card, never a sign-in prompt. This already fired in
production this morning: `[creds … 07-09 08:53:52] refresh blocked in
transit (HTTP 429) — transient` — the exact event that yesterday would have
read `credentials refused`.

**The honest 10%, named in advance:**
1. **The keychain write is unproven.** No `refreshed access token persisted`
   line exists yet (this morning's attempts were rate-limited — expected to
   decay). When the first refresh succeeds, either it persists (case closed)
   or the log shows `WRITE FAILED` — the one repair left, and it fails
   loudly, not as a mystery sign-out.
2. **Genuine Anthropic-side revocation** — always the contract's out,
   logged as `refresh REJECTED by the OAuth service`.

**Tomorrow's audit (2026-07-10):** grep the log for `[creds`. Acceptable
outcomes: `persisted` lines (proof), `WRITE FAILED` (named repair), or
transient lines with every card still live (claude's own client renews
creds at spawn regardless — worst case is a stale percentage, not a login
screen). **A card saying `needs sign-in` without a `refresh REJECTED` line
is a breach of this statement — point here.**

**Outcome (audited 2026-07-11):** the sign-in claim HELD — no login died,
no ceremony was needed. But the line this statement cited as proof of
graceful degradation (`refresh blocked in transit (HTTP 429) — transient`)
was actually root cause v3 announcing itself: the 429s never decayed,
because they were a User-Agent block, not a rate limit. "Worst case is a
stale percentage" understated what stale percentages DO — they starved the
router and inverted promise 2 for two days (see v3). Lesson for future
confidence statements: a failure line that is *expected to stop appearing*
needs a deadline; 1,706 repetitions of "transient" is a diagnosis.

## Sign-in ledger — what each ceremony bought, verified

### 2026-07-09: the /login mis-bind, and going to four pools
The morning `/login` (run inside a session, not via the panel) bound the
"clawd" FOLDER to the wrong account — austin's **Ethereum Foundation** seat
was picked in the OAuth account chooser, not clawd@buidlguidl.com. The
identity-first display caught it immediately (the card titles itself
"Ethereum Foundation" — the display contract doing its job). Net effect:
heart gained the EF pool it was missing, but the clawd team sub ended up
signed in nowhere on heart. Lesson: **the account picker in the OAuth flow
is where mis-binds happen; always glance at the card title right after a
ceremony — it shows the token's truth, not the nickname.**
Fleet target is now FOUR pools everywhere: austingriffith max 20x ·
Ethereum Foundation max 5x · clawd team · slop. Ceremonies spawned on heart
for the two it lacks ("slop", "clawdteam"); the ⚠ chips on the 🧠 page track
the rest of the fleet.

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

## Adding a sub — what you must see (written after the 2026-07-09 'limited 0%' incident)

When you complete a sign-in ceremony on any machine:
1. **Within ~30 s** the card flips live, titled by the token's real identity
   (org name · email · tier) — never by the folder label.
2. **Within one poll (~3 min, usually immediately)** the bars show the
   pool's REAL windows and accurate headroom. A brand-new or just-reset pool
   shows green, high numbers.
3. If the card instead shows a single `limited · 0%` row: that is a
   **placeholder**, not data — the usage endpoint answered 429. The card now
   says so explicitly (`usage endpoint rate-limited — backing off…`), and it
   must resolve to real numbers on its own after the Retry-After horizon.
   A `limited` placeholder that never resolves, or one WITHOUT that
   explanatory note, is a bug — point here.

**The incident:** freshly-signed-in and just-reset pools showed `limited ·
0% · checked 1s ago` indefinitely. Cause: the poller kept calling the usage
endpoint with access tokens it KNEW were expired; the junk calls fed
Anthropic's rate limiter until even honest polls 429'd, the 429 was rendered
as "100% used", and re-polling every TTL kept the limiter hot forever — a
self-sustaining lie. Fixed the same day: expired tokens are never sent
(refresh first), Retry-After is honored as a real back-off, and the
placeholder card explains itself. Note the flip side: once real numbers
return, a pool may honestly show low weekly headroom (e.g. the 20x weekly
resets on its own schedule) — honest-low is not this bug; placeholder-stuck
is.

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
- **Sessions ping-pong between pools as 5h windows heat and cool** (e.g.
  `slop → clawd (rebalance: weekly resets 7h sooner)` followed an hour later
  by `clawd → slop (plan drained)`, repeatedly). That's the
  spend-to-5–10%-left tuning working: the reset-soonest pool is drained hard,
  walls or goes hot, sessions evacuate, its window resets, they come back.
  Every hop is a seamless `--resume` (same cid, same viewers); the churn
  costs a few seconds of respawn per hop, capped by `HANDOFF_COOLDOWN`
  (10 min/session). If the hop rate ever bothers, raise
  `SUB_REBALANCE_MARGIN` or lower `SUB_HOT` — it's tuning, not a bug.
- A signed-out login shows its **last cached** windows (or a synthetic
  `limited` row) — historical, not live.

## When it breaks, gather this (then point the agent here)

1. Screenshot the 🧠 page (h.atg.link, after a hard reload).
2. On the affected machine: `python3 tools/usage_probe.py <config_dir>` for
   the login in question (empty arg = `~/.claude`).
3. `grep -i "handoff\|account" ~/Library/Logs/clawd-harness.log | tail -30`
   — did the router try? what did it see?
3b. **Routing to the wrong pool specifically:** check usage `checkedAt`
   ages in `.clawd-harness.sessions.json` before reading any routing code —
   stale snapshots (> ~9 min) silently drop accounts from the candidate
   set (root cause v3). A wall of `refresh blocked in transit (HTTP 429)`
   lines = the refresh client is being identity-blocked again.
3c. **A rate limit was SEEN (banner flashed / message bounced):** grep the
   log for which layer caught it, in order — `limit banner in the PTY`
   (tripwire), `send got NO hook in` (send watchdog; its `pty tail:` dump
   shows exactly what the terminal displayed — if a real banner is visible
   in that dump but the tripwire line is absent, the banner regex missed
   and the dump is the repro), `prompt bounced off dead plan` (rescue ran),
   `busy but hook-silent … treating as stuck` (only the 10-min sweep caught
   it — everything faster missed; that's a bug, bring the pty tail). A
   banner with NONE of these lines within ~30 s = the wall wasn't detected
   at all — breach, point here.
4. Note which machine the stuck session was on and which pools that machine
   held at the time (promise 3 is per-machine — "there was headroom on
   another box" is the known limit, not a breach).
5. `curl -s https://h.atg.link/ | grep -c orgUuid` — is the served UI current?
