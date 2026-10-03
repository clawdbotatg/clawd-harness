# Unified "all sessions" view — design note

> Status: **plan / not yet built.** Captures the agreed direction for reorienting
> the harness UI around *all open sessions across all projects* (and, in fleet
> mode, all machines) instead of one-project-at-a-time. An open session = a job in
> progress; the harness should be a place to glance across those jobs and hop
> between them, not a drill-down through project → session → terminal.

## The core shift: depth stack → session-centric

Today navigation is a **depth stack** — `LEVELS = ['projects','sessions','tty']`
(`index.html:3645`); horizontal swipe / arrows move *between depths*
(`goDeeper`/`goShallower`, `index.html:3674-3675`). The session you see is scoped to
the selected project (`sessionList.filter(s => s.pid === currentPid)`,
`index.html:3666`).

We flip to a **session-centric** model:

- **One primary surface** — the focused session (live tty / transcript) fills the
  screen, with a **session rail** strip across the top listing *every* alive
  session (all projects; all machines in fleet), sorted by importance (see below).
  Each rail chip shows a status LED + short title; the active chip is highlighted.
  This is the default landing.
- **Horizontal = cycle sessions** (no longer depth). Swipe L/R on mobile,
  `Ctrl+Shift+←/→` on desktop, move prev/next through the rail.
- **Projects becomes an overlay**, not a swipe-level — reached via a header
  `Projects` button (always visible), `Ctrl+Shift+P`, or swipe-up from the top
  edge. It's where you filter the rail to one project and create new sessions.

### Why the data work is small

In **direct mode** the client already holds every session across all projects in
`sessionList` (`index.html:767`); it only narrows to `currentPid` at dive-time
(`index.html:3666`). The server already sorts `last_active DESC`
(`server.py:1244-1245`). In **fleet mode** sessions are already merged across
machines via `perMachine` (`index.html:775`) / `unifiedProjects`
(`index.html:776`). So the "all sessions" data is already in the browser — this is
mostly a **navigation + UI rework**, not a data-model change.

## Navigation model: before → after

| Gesture | Today | After |
|---|---|---|
| Swipe L / `Ctrl+Shift+→` | dive deeper (proj→sess→tty) | **next session** in rail |
| Swipe R / `Ctrl+Shift+←` | climb out | **prev session** in rail |
| `Ctrl+Shift+↑/↓` | cycle session within project | free (candidate: jump to first/last, or rail page) |
| Projects | climb to top level | header button / `Ctrl+Shift+P` / swipe-up overlay |
| `Ctrl+Shift+W` | close session | close → focus neighbor in rail |
| `Ctrl+Shift+N` | new session | new-session flow (see below) |

`cycleSession` already exists — change its scope from `here` (current project) to the
**rail set** (all sessions, or the active project filter) and bind it to L/R.

## UI components

1. **Session rail** — extend the existing `#sessionbar` tab strip
   (`index.html:442`). Drop the per-project filter so it shows the full set;
   horizontally scrollable; active chip highlighted; click a chip to focus. Chip =
   status LED (reuse `.sdot` busy/blocked/idle, driven by `meta().status`) +
   truncated title. In fleet, render across machines (data already in
   `perMachine`/`unifiedProjects`).
2. **Close-X on the focused session** — a large, obvious **✕ in the top-right of
   the tty pane** that closes the focused session (wraps the existing
   `closeCurrentSession`, today only on `Ctrl+Shift+W`). After close, focus the
   rail neighbor. This is the primary "I'm done with this job" affordance — keeps
   the rail from filling with finished work.
3. **Projects overlay** — reuse the existing projects rung (cards + add-row) as a
   slide-in overlay. Adds an **"All sessions"** entry (clears the filter) and a
   per-project **"+ New session"**. Selecting a project sets a rail filter (rail
   narrows to that project) and returns to the session plane.
4. **Header** — `[≡ Projects ▾]` on the left, a filter label in the middle
   ("All sessions" / "&lt;project&gt;"), `[+ New]` on the right. Replaces the
   breadcrumb-driven depth affordance.

## Importance sort (the rail order)

With every session in one list, idle jobs would bury the ones that need attention.
Sort by **importance bucket, then recency** — the signals already exist in
`ClaudeSession.meta()` (`server.py:362-379`):

1. **Needs you** — `waiting` / `status === 'blocked'` (turn ended asking the human;
   `blocked_on` is the question). Most urgent — floats to the front.
2. **Working** — `busy` / `status === 'working'` (turn in flight).
3. **Idle** — `status === 'idle'`, sorted `last_active DESC`.
4. **Dead** — `alive === false` → bottom (candidate to hide behind a toggle).

Within each bucket, secondary sort by `last_active DESC`. This can be done
server-side in `_ordered()` (`server.py:1244`) so every client agrees, or
client-side in the rail render — leaning server-side for one source of truth.

A later enhancement (Phase 5) could add a **filter chip** ("Working only" /
"Needs you") if buckets alone aren't enough.

## Routing (`parseHash`/`syncUrl`/`resolvePendingNav`, `index.html:3514`/`3532`/`3553`/`3582`)

Keep `cid` routable so notification deeplinks keep working:

- `#/` → session plane, all-sessions filter, focus most-recent.
- `#/s/<cid>` → session plane, focus `<cid>` (filter = all). *(new short shape)*
- `#/p/<key>` → rail filtered to project, focus its most-recent.
- `#/p/<key>/s/<cid>` and fleet `#/m/<machine>/p/<key>/s/<cid>` → **kept as-is**
  (back-compat + the `docs/DEEPLINKS.md` builder unchanged); resolve to the session
  plane, rail filtered to the project, focused cid.
- Projects overlay is **transient** (not a route) to keep routing simple.

`LEVELS` / `stepLevel` / `goDeeper` / `goShallower` collapse to one content view
(`session`) plus an overlay toggle; `setView` / `currentView` simplify accordingly.

## Mobile vs desktop

- **Default landing (both):** session plane, focused on the most-important session
  (top of the sorted list). Unifies the current split (desktop→tty, mobile→
  transcript).
- **Mobile:** swipe L/R cycles sessions; swipe-up *from the top edge / header* opens
  Projects (edge-zone only, to avoid fighting tty scroll — header button is the
  reliable primary path). Read-only tty on touch stays (`disableStdin:isTouch`);
  composer sends to the focused session. The close-✕ is the touch-friendly way to
  end a job (no `Ctrl+Shift+W` on mobile).
- **Desktop:** `Ctrl+Shift+←/→` cycles; `Ctrl+Shift+P` opens Projects; rail is the
  always-visible strip.

## Fleet subtlety

Cross-machine cycling means focusing a session on another machine must
`attachMachine()` + ensure the E2E channel (`e2eEnsure`) + subscribe before its live
tty streams — a handshake with latency on each cross-machine hop. The rail *chips*
render instantly from the `perMachine` cache; only the focused tty pays the attach
cost. Show a brief **"connecting…"** state on the focused pane during the hop.

## New-session flow

Creating a session needs a project. So `+ New` / `Ctrl+Shift+N`:
- **No active filter** → open the Projects overlay to pick a project (or default to
  the last-used project).
- **Project filter active** → create directly in that project.

Either way the server's `focus` reply (`pendingNewFocus`) drops you onto the new
session in the rail — same mechanism as today.

## Phasing (each independently shippable)

1. **Rail data** — unfilter `#sessionbar` to all sessions (+ fleet cross-machine
   merge). Low risk, visible win.
2. **Importance sort** — bucket order (needs-you → working → idle → dead) in
   `_ordered()`; rail reflects it.
3. **Rebind nav** — L/R → `cycleSession` over the rail set; add `Projects` button +
   `Ctrl+Shift+P`. Keep depth working until removed.
4. **Close-✕ on tty** — wrap `closeCurrentSession`; focus neighbor after close.
5. **Routing + LEVELS collapse** — session plane + projects overlay; add `#/s/<cid>`;
   keep old shapes resolving.
6. **Default landing** — most-important session on boot, both platforms.
7. **Polish** — rail overflow scroll, swipe-up overlay (mobile), cross-machine
   "connecting…" state, optional "Working only" / "Needs you" filter chip, hide-dead
   toggle.

## Deferred / out of scope for now

- **Grid overview** — sticking with the rail strip for now; revisit a zoomed-out
  grid of full cards (digests, `blocked_on`) only if the rail proves cramped for
  scanning many jobs.
