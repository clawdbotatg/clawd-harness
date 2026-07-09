# GOAL.md — the two things this system must deliver

Austin's words, 2026-07-09. Everything in [EXPECTATIONS.md](EXPECTATIONS.md)
exists to serve these two goals. If either is violated, that's the bug to
drop everything for.

## Goal 1 — sign in once, not every day

**I should not have to log in to every account every day.**

One OAuth ceremony per (subscription × machine), ever. The harness keeps
every stored login fresh itself — idle or busy — for the life of the
refresh grant (~27 days rolling). A login may only ever need a re-sign-in
when Anthropic's own OAuth service rejects the grant (logged as
`refresh REJECTED by the OAuth service`); a failed refresh for any other
reason (Cloudflare block, rate limit, outage) must never be shown as
"signed out".

- **Violated when:** any card says `needs sign-in` without a
  `refresh REJECTED` line in that machine's log.

## Goal 2 — never rate-limited while any sub has headroom

**I should never get a message that we hit the subscription rate limit if I
have another subscription with headroom.**

New sessions always spawn on the best eligible pool — among pools with room,
the one whose **weekly window resets soonest** (use-it-or-lose-it: drain the
soonest-resetting sub first, headroom only breaks ties); running
sessions automatically hand off to a better pool when theirs drains —
before the wall when possible, and at worst one turn ends early and the
session heals itself before the next message. This requires every
subscription to be signed in on every machine (handoff is per-machine) —
the ⚠ chips on the 🧠 page are the standing to-do list that protects this
goal.

- **Violated when:** a session sits stuck on a rate-limit message while a
  pool with headroom is signed in **on that same machine**.

When either goal is broken: screenshot the 🧠 page, grab
`~/Library/Logs/clawd-harness.log`, and point the agent at this file and
EXPECTATIONS.md.
