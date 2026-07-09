# Accounts panel — what you *should* see, and how to fix it when you don't

The accounts panel (foot of each machine's projects rung; one column per
machine in the fleet UI) renders one **card per local account**. This doc is
the reference for what a healthy panel looks like, the invariants that must
hold across machines, and the runbook for when they don't. Companion to
[SUB-ROUTING.md](SUB-ROUTING.md) (how the router *uses* these numbers).

## The one mental model that prevents all confusion

**A card's name is a local folder label. Its graphs belong to whatever
subscription is signed in inside that folder.**

- The name (`ef`, `clawd`, …) is just `~/.clawd-accounts/<name>` on that one
  machine. Nothing syncs names across the fleet — `ef` on three machines is
  three independent credential stores that merely share a label.
- The bars are polled live from Anthropic's OAuth usage endpoint for the
  signed-in **subscription**. Usage is server-side state: the machine
  displaying it contributes nothing to the numbers.
- The subscription is identified by the **organization uuid**, *not the
  email*. One email can hold seats in several orgs (e.g. a personal Max 20x
  *and* an employer-provisioned seat) — same email, disjoint usage pools,
  different plans. The card's identity line shows `org · email · tier` for
  exactly this reason: the email alone cannot disambiguate.

From that, the invariants:

1. **Same org signed in anywhere ⇒ same graphs.** Two live cards bound to the
   same org — different machines, different names, doesn't matter — must show
   identical percentages and reset times, within poll staleness (compare the
   "checked Ns ago" stamps). If they don't match, one of them isn't actually
   on that org.
2. **Different orgs ⇒ independent graphs.** Different plan tier (`max 5x` vs
   `max 20x`), different reset dates, unrelated percentages — all normal.
3. **A name is only as meaningful as you keep it.** If `ef` is supposed to
   mean "the Ethereum Foundation seat" fleet-wide, that's a convention you
   enforce at sign-in time (pick the right workspace in the OAuth flow) — the
   harness can't know which org a name was *meant* for.

## Anatomy of a healthy card

```
┌ Ethereum Foundation                             41% ┐   ← POOL identity · headroom
│   austin.griffith@… · max 5x · “ef”                 │   ← email · tier · local nickname
│   5h       ████████████████████   99% left  ↻ in 3h │   ← 5-hour session window
│   7d       ████████░░░░░░░░░░░░   41% left  ↻ in 6d │   ← 7-day all-models window
│   7d fable █████████░░░░░░░░░░░   44% left  ↻ in 6d │   ← 7-day Fable/Opus window
│   ▸ next spawn · 1 live session      checked 1s ago │
└──────────────────────────────────────────────────────┘
```

Since 2026-07-08 the **title is the pool's identity** (org name; the
auto-generated `<email>'s Organization` demotes to the email's local part)
and the local nickname is small print in quotes — shown only when it differs
from the identity. Nicknames proved themselves liars (head's “clawd” held
austin's login for weeks); they remain visible because they're still the
re-add key and the sign-in session name, but they never headline. The
machine header's router line (`new sessions: most headroom → …`) names the
pool the same way.

- **Big number** = headroom (100 − usage of the binding window); its label
  names which window binds. Card highlight + "▸ next spawn" = the router's
  pick for the next session on that machine, echoed in the machine header
  ("new sessions: most headroom → ef"). One highlighted card per machine.
- **Windows**: rolling rate-limit buckets with `% left` and reset time. The
  reset times are subscription facts — another correlation check: same org ⇒
  same resets.
- **checked Ns ago**: poll staleness. Live credentials poll frequently
  (seconds); a card stuck at "checked 8h ago" means polling stopped — always
  paired with a broken/pending status, never with live bars.

**A signed-out card is *supposed* to look dead**: `⚠ signed out — needs
re-sign-in`, a flat `LIMITED / 0%` placeholder instead of graphs (the poller
can't read usage without a working token), a frozen "checked" age, and a
"sign in again" button. It is excluded from routing. This is correct display
of a revoked credential, not a rendering bug.

## Runbook: symptom → cause → fix

**Same name shows different graphs on different machines.**
Not a sync bug — the name is bound to *different orgs* on those machines (or
one is signed out). Read each card's identity line: if the orgs differ,
that's the whole story. Fix: on the machine wearing the wrong org, hit
"sign in again" and **pick the intended workspace** in the OAuth org chooser
(same email — the org step is where it went wrong last time).
To verify from a shell instead of the UI:
```bash
python3 -c "import json;oa=json.load(open('$HOME/.clawd-accounts/ef/.claude.json')).get('oauthAccount',{});print(oa.get('emailAddress'),'|',oa.get('organizationUuid'))"
```
Caveat: `.claude.json` can be stale after a re-login (the keychain updates,
the json doesn't) — the server's displayed identity is token-bound via the
OAuth profile endpoint and wins on conflict. Trust the card over the json.

**Two cards on ONE machine show identical bars.**
Same org double-mounted under two names. Harmless to usage accounting
(it's one pool either way) but it fools routing diversity: the router sees
two "accounts" that drain together. Re-sign one into the org it was meant
for, or delete the redundant account dir.

**A card shows a plan/tier that doesn't match what you expect for that
login.** The email is shared across orgs and the card is on the *other* org
(see above), or the plan genuinely changed. The tier shown comes from the
token's live profile, so believe it — realign the sign-in, not the display.

**Graphs differ slightly for cards on the same org.** Compare "checked"
ages first — a card checked 8h ago is a snapshot, not a live disagreement.
Only matching-freshness mismatches are real (and then: different orgs).

**Card says "no account data — machine offline, or its harness predates
accounts".** The machine's harness needs a `git pull` (auto-pull does this
within ~5 min unless the worktree is dirty or `AUTO_PULL=0`).

### Worked example (2026-07-08, the `ef` incident)

Three machines showed three different `ef` cards: **head** — signed out
(dead token, placeholder card: correct display); **heart** — live, `max
20x`, 2% headroom, bars identical to head's `clawd` card ⇒ actually signed
into the personal org, mis-bound at sign-in; **leftclaw** — live, `max 5x`,
41% ⇒ the real EF seat. Diagnosis took one glance once org names were on the
cards (they were added because of this incident). Fix: re-sign head's `ef`
and heart's `ef`, choosing "Ethereum Foundation" in the workspace picker.
