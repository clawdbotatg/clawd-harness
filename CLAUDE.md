# clawd-harness — orientation for Claude

A web **harness** for driving interactive (subscription-billed) Claude Code
sessions from a browser. Forked from `clawd-console`. `README.md` is the
user-facing overview; this file orients an agent working **on** the code.

> **Relationship to `claude-p-agent`** (`projects/claude-p-agent`): that repo is the
> generic agent pattern — `claude -p` + a `CLAUDE.md` persona + trust-tagged channels,
> with a build tool that delegates real coding to managed Claude Code sessions. **This
> harness is the worker/session manager that build tool delegates to:** when the agent
> runs its `code` helper, it drives this harness (over the WS protocol) to spawn and
> supervise the real `claude` worker sessions. claude-p-agent is the brain pattern;
> the harness is the engine behind its hands. (The live voice agent's adapter is
> `projects/clawd-video-chat`.)

> **Multi-machine?** The fleet (relay/worker layer that drives N harnesses from
> one phone via a public relay) now lives **in this repo** under **[`fleet/`](fleet/)**
> — see **[`fleet/CLAUDE.md`](fleet/CLAUDE.md)**. It was folded in from the former
> standalone `clawd-fleet` repo (now archived). The **boundary is preserved as a
> code-discipline rule**: `fleet/` code must never import `server.py` or reach into
> harness internals — the worker is just another WS client. The wire contract it
> depends on is **[`docs/WS-PROTOCOL.md`](docs/WS-PROTOCOL.md)**; keep that doc in
> sync if you change the WS protocol in `server.py`. Fleet deep docs:
> **[`docs/fleet/`](docs/fleet/)**. **Adding a machine to the fleet?** Hand the new
> box **[`docs/fleet/ADD-MACHINE.md`](docs/fleet/ADD-MACHINE.md)** — a self-contained
> checklist (harness + worker + the E2E `cryptography` dep and shared passkey file).
>
> **`index.html` is the single, *unified* UI** shared by both modes — mode-aware via
> `window.__FLEET__`: the harness serves it untouched (direct mode); the fleet relay
> (`fleet/relay.py` `_serve_file`) injects the flag for fleet mode (machines rung +
> passkey). There is now **one copy** (the old cp-to-fleet sync ritual is gone). To
> change the fleet UI: edit this `index.html`, then `git push` + `git pull` on the
> box — the box is now a **git checkout** at `~/clawd-harness`, not an scp dir (see
> `docs/fleet/DEPLOY.md`). The fleet/passkey code here is dormant unless `__FLEET__`
> is set.

## Run / test

> ### Definition of done: `python3 tools/shipcheck.py`
> **A change is not done when the file is saved, and not done when you have
> screenshotted it working. It is done when `shipcheck` exits 0.** Run it before
> you tell the user a UI change is live — it verifies tree-clean + HEAD-pushed +
> `h.atg.link` serving HEAD's `index.html` byte-for-byte (`--wait` blocks through
> the relay's ~3min pull).
>
> This is written as a hard gate because the trap is *designed* to feel like
> success. Saving `index.html` hot-reloads the browsers on **this** box in ~1s,
> and `tools/uiprobe.mjs` drives `127.0.0.1:8787` — so the local loop produces a
> real, correct screenshot of the new UI **from the working-tree file, committed
> or not**. Every signal says shipped; the phone still shows the old page,
> because production is the fleet and the fleet only moves on `git push`.
> Leaving the tree dirty is worse than neutral: it makes `auto_update_loop` skip
> this box, so it also blocks *everyone else's* pushed changes from landing here.
> The standing "only commit when asked" default does not apply to this repo —
> here, push **is** the deploy, so "make the UI do X" means in production.
>
> Do not report a change as live on the strength of a local screenshot. That is
> exactly the evidence that has been wrong every time.

- `python3 server.py` → prints a tokenized URL `http://127.0.0.1:8787/?t=<token>`
  (token persisted in `.clawd-harness.token`; or set `CONSOLE_TOKEN`).
- **Deploy to the fleet = `git push` to main.** Every harness runs
  `auto_update_loop` (~5 min + jitter): on-main + clean-worktree + ff-only →
  it pulls itself, then the file watcher hot-reloads browsers (index.html) or
  gracefully restarts (server.py). A dirty worktree (live-edit in progress) is
  skipped. Opt a box out with `AUTO_PULL=0`. No ssh, no manual pulls.
  **The relay box now self-updates too** (2026-08-07): it runs no harness, so
  it used to be the one box nothing pulled — and it's the box that *serves the
  fleet UI*, so a pushed UI change stayed invisible on `h.atg.link` until a
  human remembered one ssh (it once sat 8 commits behind while every harness
  was current). A systemd timer (`fleet/deploy/clawd-fleet-pull.timer` →
  `relay-pull.sh`, ~3 min, same on-main/clean/ff-only guards) closes that.
  UI-only changes need no restart (`index.html` is read per request); a
  `fleet/*.py` change restarts relay+worker automatically; a `controller/`
  change only WARNS in the journal, because restarting kills a running PM turn
  — do that one by hand when quiet (`journalctl -u clawd-fleet-pull`). See
  `docs/fleet/DEPLOY.md`.
- Daemon: `./daemon.sh install [WORKDIR]` (launchd, RunAtLoad + KeepAlive,
  re-`--resume`s sessions). Also `status | logs | restart | uninstall`.
- Smoke test: `python3 smoke_test.py` (reads the token file; asserts both channels).
- **Port 8787**, launchd label **com.clawd.harness**. (clawd-console uses 7878 /
  com.clawd.console — they coexist on purpose.)
- **Is it running?** It usually already is (launchd `KeepAlive`). Don't check
  with `pgrep -f server.py` — launchd's invocation doesn't arg-match, so that
  returns nothing even while it's up. Use `launchctl list | grep clawd.harness`
  (shows the PID) or `lsof -nP -iTCP:8787 -sTCP:LISTEN`. So: edit `index.html` →
  it live-reloads (see below); no need to start a server first.
- Needs the `claude` CLI on a Claude **subscription** (OAuth, not an API key).
  Pure Python stdlib; xterm.js loads from a CDN.
- Verify JS edits: extract the `<script>` from index.html and `node --check` it.
  The app has been verified live in Chrome via the **LAN URL** (see stale-cache note).
- **Watch the UI run yourself — `tools/uiprobe.mjs`.** For any *visual/DOM* bug
  (textarea sizing, layout, a button that won't repaint) don't reason blind: drive
  the running app from a **local headless Chromium** and read the real DOM.
  `cd tools && npm i` once (playwright browsers are already cached on this machine,
  so it's just `playwright-core`), then
  `node uiprobe.mjs` (snapshot + screenshot the projects rung) or
  `node uiprobe.mjs --hash '#/p/self/s/<cid>/tty' --box` (deep-link a session and
  assert the composer grows on fill + shrinks on clear — prints `{resting,tall,cleared}`,
  exit-codes for a verify flow; it clears the box rather than sending, so no real
  message hits a live session). **Why this and not the `claude-in-chrome` MCP
  browser:** that browser is *remote* (a cloud Chrome on another network) and
  **cannot reach `127.0.0.1:8787`** — every navigate fails `ERR_CONNECTION_REFUSED`.
  A process launched from the Bash tool is on *this* machine (same as `server.py`),
  so it can. This is the loop that turns "guess a fix, commit, ask the human to
  eyeball it" into "run it, read the number."
  **It proves the code, not the deploy.** uiprobe renders the *working-tree*
  file on *this* machine, so it goes green on uncommitted work. A green uiprobe
  plus an unpushed commit is the exact combination that has repeatedly produced
  a false "it's live." Pair it with `tools/shipcheck.py`.
- **`tools/pmprobe.mjs`** — the same idea for the **PM tab**: it stubs every
  `/pm/*` call (so it needs no live controller and never touches a real session),
  holds `/api/chat` open to simulate a turn mid-think, then asserts the tab is
  still drivable — switch threads, ＋ new, and that the reply lands in the thread
  that *sent* it rather than whichever one you switched to. Guards the 2026-08-08
  "PM tab freezes while it's thinking" bug from the client side;
  `controller/test_pm_responsive.py` guards the server side.
- **`tools/fleetprobe.mjs`** — the same idea for **fleet mode**, which uiprobe
  can't reach (it drives the harness directly, where `FLEET` is false). It reads
  `index.html` off disk with the relay's own `__FLEET__` injection and stubs
  `window.WebSocket`, so it needs no relay, no worker and no passkey, and nothing
  leaves the machine. Asserts the two things that decide how many passkeys you
  pay at dawn: a **switched-off machine receives zero frames** (see "active
  machines" in `fleet/CLAUDE.md`), and the relay's 20s roster heartbeat **can't
  close a machine's in-flight passkey modal**. `fleet/test_relay_prefs.py`
  guards the server half.
- **`tools/settingsprobe.mjs`** — fleet-mode probe of the **⚙️ settings** page,
  currently the "default machine for new projects" select. It asserts the whole
  chain, not the widget: the choice persists, re-labels the projects rung's
  `default/all` option, and is what `ensureTargetMachine()` targets — a select
  that stores a value nothing reads would look perfectly fine. Same fake-relay
  stub as fleetprobe (no relay, no worker, no passkey).
- **`tools/rungprobe.mjs`** — guards the **projects rung against its own
  repaint** (2026-08-09). The rung repaints on every `projects` frame, so it
  must hold still: scroll position survives, focus is never taken, and text
  still in the `<input>` but not yet mirrored into `projectFilter` isn't
  dropped. It calls `renderProjects(projectList)` — literally what a frame does
  — so it touches no session. See "Repaint, don't rebuild" below.
- **`tools/splashprobe.mjs`** — guards the **splash cooldown** (2026-08-09). The
  session-entry splash (the RSVP flash of project → machine → title → tldr) is
  worth ~2s only when you've been *away* from that session; hopping between two
  tabs re-ran it every hop. Each session now carries a last-looked-at stamp,
  refreshed every 30s while its tty view is visible (a hidden tab isn't looking)
  and on climbing out, persisted in `localStorage` (`cc_seen_at`, pruned to the
  window) so a reload doesn't re-flash what you're staring at; `maybeSplash`
  fires only past `SPLASH_COOLDOWN_MS` (10 min). The probe lands on the sessions
  rung and drives `maybeSplash()` against a *fake* session in `sessionList`, so
  it subscribes to nothing and claims no PTY size.
- **`tools/deadveilprobe.mjs`** — guards the **dead veil** (2026-08-16): a
  session that dies (exit frame) or vanishes from the registry (its cid stops
  appearing in `sessions` frames) used to leave the tty view a silent black
  void — the "long dead tty view" mystery; the only tell was tiny meta-line
  text. Now a big splash-style island says **"ended"** (dead but still listed —
  tap dismisses so any final output behind stays readable) or **"gone"** (cid
  unknown), re-judged on every sessions frame and silent until a snapshot
  exists so boot/reconnect gaps never mislabel a live session. The probe
  asserts both words, splash suppression on dead sessions, tap-parks-per-cid,
  re-arm on fresh entry, and the exit-frame path — against a fake session with
  `inSessionView` stubbed, so it subscribes to nothing and claims no PTY size.
- **`tools/sentlogprobe.mjs`** — guards the **🕘 sent history** (2026-08-26),
  the companion to gotcha #2's bracketed paste: delivery of a long send is
  INTACT (verified even mid-turn and mid-Bash-tool — the checklist that
  "vanished" on 08-26 was in fact fully received and acted on), but claude's
  TUI echoes only the TAIL of a long paste, and a send delivered while a turn
  runs rides in as steering with NO user-message echo (it may not even appear
  in the transcript) — to a person who just dictated a wall of text that reads
  as "it cut off my message and I can't get it back". Every prior recovery net
  (draft, outbox, pending) clears ON delivery by design, so the harness now
  archives every composer send in a localStorage ring (`cc_sent`, 30 entries)
  behind the 🕘 button beside the composer — view, copy, or ↩ restore. The
  probe asserts deliverSend records the full text (quick chips excluded), the
  modal shows head AND tail, ↩ restores verbatim, and the ring caps. Runs on
  the sessions rung with `hsend` stubbed — nothing touches a real session.
  (Server-side twin: `.clawd-harness.prompts.jsonl` logs every browser send in
  full — that's where a lost text is recovered from on any OTHER device.)
- **`tools/tabfilterprobe.mjs`** — guards the **🔎 tab-strip filter** (2026-08-09):
  that it sits at the far right, stays pinned there when the strip scrolls (it's
  `position:sticky`, so tabs pass *under* it), that typing narrows the strip and
  clearing restores it, and — the one that bites — that a `renderSessionBar()`
  repaint keeps the live `<input>`, its focus, and text the `input` event hasn't
  mirrored yet. It lands on the **sessions rung** (`#/p/self`), where the strip
  already renders, so it subscribes to nothing and claims no PTY size.
- **`tools/pinfilterprobe.mjs`** — the same 🔎 for the **📌 pin board**
  (2026-08-11): the filter in `#pinhead` narrows cards by title / tldr / digest /
  🧪 test hint / ⏳ blocked-on / project (and, fleet, machine). The board is the
  one rung that still legitimately repaints with `innerHTML=''` (cards carry live
  per-card prompt inputs, so they're rebuilt wholesale), which is exactly why the
  filter box lives **outside** `#pinboard` — the probe asserts that placement,
  the n/total count in the title, AND-narrowing, repaint survival of focus +
  un-mirrored text, and Enter-on-one-hit opening the pin and clearing the
  filter. It lands on `#/pins` and filters **fake** pins injected into
  `sessionList` (the splashprobe pattern), so it touches no real session.
- **`tools/voiceprobe.mjs`** — guards the **🎙 voice PM** (2026-08-16): a
  **nervous triple-tap during the seconds-long connect mints exactly ONE
  session** (the day-one prod chaos: each extra tap minted another full
  realtime session and the sessions answered each other's speaker output —
  the claim is now synchronous, taps mid-connect are ignored, and the mic
  demands echo cancellation), **the mic is hard-muted while assistant audio
  actually plays** (half-duplex via `output_audio_buffer.started/stopped` —
  browser AEC is best-effort and on real speakers the model heard itself,
  interrupted itself, and looped; 🎧 headphones mode on the HUD buys voice
  barge-in back, and tapping the HUD state word interrupts by hand —
  `response.cancel` + `output_audio_buffer.clear`), the button goes LIVE, a
  tool-call event executes against the right
  `/pm` endpoint and sends **both** data-channel events back (drop
  `response.create` and the model knows the answer but never speaks it),
  transcripts land as feed bubbles, the session survives leaving the PM view,
  and hang-up stops the mic. `/pm/*`, `api.openai.com`, `getUserMedia`, and
  `RTCPeerConnection` are all stubbed — no key, no mic, no live controller.
  Server half: `python3 -m controller.test_voice`.
- **`tools/ironprobe.mjs`** — guards the **🔥 irons layer** (2026-08-26): the
  header icon sits immediately left of the 🗂️ projects icon and opens
  `#/irons`; creating an
  iron sends an **irons-only** relay `prefs` frame (a frame that also carried
  `inactive` would clobber the machines deny-list — the relay merges
  per-field, `fleet/test_relay_prefs.py` guards that half); a project card's
  🔥 opens the modal picker and assignment stores the cross-machine
  projectKey; tapping an iron dives STRAIGHT into its warmest session (no
  detail page) with the one-row `#ironrow` chrome + the scoped strip (every
  member session across machines, 📌 pinned at the END, outsiders excluded),
  tab taps switching the terminal in place; ✎ edits in an overlay without
  leaving the tty; an empty iron opens the add-project picker over the list;
  the ONE static box (filter + create-name — no separate create form exists)
  survives a repaint with focus + un-mirrored text, arrival focuses it,
  typing narrows by title, Enter on a lone match dives in and clears, and
  ＋ create names an iron from the same text;
  the list is a PRIORITY order (new iron on top, ⠿ drag persists as an
  irons-only push, grabbing the handle never opens the iron);
  and in direct mode the harness `irons` frame drives the same dive +
  assignment goes out as `ironAssign` + reorder as `ironOrder`
  (registry-backed — `python3
  test_irons.py` guards that half). Both modes run against the stubbed
  WebSocket; no session touched.
- **`tools/spawnprobe.mjs`** — guards the **spawn watch** (2026-08-20): a `new`
  that never comes back must not be a black void, and the prompt typed into it
  must never be lost. `newSession()` drops into the tty view *before* the
  server's `focus` names a cid (by design — it blanks the leaving session's
  screen); in fleet mode the `new` rides the machine's E2E channel, so a channel
  still securing (or a worker/harness that's gone) left a cursor on black with
  no cid, no name, and no way to tell slow from dead. The day it bit: a viewer
  back from a long idle, the fleet-wide resume round stalled ~90s (see
  `fleet/CLAUDE.md`, `E2E_RESUME_REPLY_MS`), ＋ and a prompt went into that
  gap, a reload later both were gone. Now `armNewFocusWatch` paints a
  "starting…" island (the dead-veil element, accent-coloured) the moment we
  ask and, if no focus lands in `NEW_FOCUS_WAIT_MS` (30s), stands down loudly:
  purges the undelivered new/send from the channel queue (no ghost session
  minutes later), climbs back to the sessions rung, and hands the text back to
  the composer. Two text-loss holes closed with it: `flushPendingSend`'s 8s
  fallback used to fire the queued text at a **null cid** and spend the
  reload-recovery copy doing it (now it holds until a session exists), and a
  reload landed on the sessions rung **before the projects frame**, so the
  composer context was the placeholder `new:_` — it took the pending text,
  then the switch to `new:<pid>` swept it into a draft slot nothing reads
  (now the placeholder never takes it, and anything already swept is handed
  back once). Direct mode against a stubbed WebSocket that never answers a
  `new` — no harness, no relay, no session touched.

## Architecture (one server, multi-project, multi-session)
- **server.py** — a `SessionManager` owns N `Project`s and N `ClaudeSession`s.
  A **project** is a git repo under `projects/` (`PROJECTS_DIR`, gitignored); a
  session's `claude` runs with `cwd` = its project's path (`ClaudeSession.pid` →
  `Project`). Each session is one interactive `claude` in a PTY (no `-p`), with
  its own transcript tail + ring buffer. `cid` = stable console id (ours;
  survives claude's id rotation); `session_id` = claude's id (rotates on
  compaction/resume). Registry persisted to `.clawd-harness.sessions.json` as
  `{"projects":[…],"sessions":[…]}` and `--resume`d on restart. **Disk is the
  source of truth for the project list:** `reconcile_projects()` runs on the
  ~1s `watch_ui` loop (and on boot) — it adopts any new git repo under
  `projects/` and drops any whose folder has vanished (killing its now
  cwd-less sessions), broadcasting the change. The registry persists projects
  only as a pid↔path memo so ids stay stable across reboots.
- **Projects layer:** create a new **public** repo under `GH_OWNER`
  (`clawdbotatg`) via `gh repo create … --clone`, or clone a repo — both run
  async in a thread with a `cloning → ready|error` status broadcast. Clone input
  is normalized: a full git URL/path is used as-is, while `owner/repo` and a bare
  `repo` name are resolved against `github.com` (bare → `GH_OWNER`), so typing
  `slop-computer-live` clones `github.com/clawdbotatg/slop-computer-live`.
  **Creation needs `gh` authenticated in the server's environment** (cloning a
  public URL does not). **There is no in-app "remove" for gh projects:** to drop
  one you delete its repo folder under `projects/` yourself and the reconcile
  loop follows within ~1s (the pinned self-project lives outside `projects/`, so
  it's never touched). **Third kind: local private projects** (`kind:"local"`,
  violet in the UI vs the amber that means clawdbotatg repo) — a
  folder anywhere on the machine's disk, registered via `addLocalProject{path}`
  (the 📁 button). A path that doesn't exist yet round-trips a confirm
  (`localProjectMissing` frame → UI are-you-sure naming the absolute path +
  machine → retry with `create:true` → `mkdir -p`); every path guard runs
  before any mkdir, so create can't be aimed at `/`, `~`, `projects/` or the
  harness dir. Test: `python3 test_local_create.py`. Lives only in the registry (invisible to the disk reconcile),
  detached via `removeProject{pid}` (the card's ⏏ — never touches the folder),
  and never auto-dropped: a missing path flips it to error after `LOCAL_GONE`
  (~30s) and heals when the path returns. Privacy is a **harness-level
  guarantee**: no gh/git-remote operations ever run against it, `repoUrl` is
  forced `""` in the Project ctor, and its path leaves the machine only inside
  E2E fleet frames. (The session inside it is still a full `claude` agent — the
  guarantee is about the harness, not a sandbox.)
  **Fourth kind: external repos** (`kind:"external"`, teal, the 🔀 button —
  2026-08-22): someone else's GitHub repo. `addExternalProject{repoUrl}` runs
  one `gh repo view --json viewerPermission,…` in the provisioning thread and
  decides from it: no push access → `gh repo fork <url> --clone` (origin = our
  fork under `GH_OWNER`, upstream = the source); push access → plain clone
  with an `upstream` remote added, so every external project has the same
  remote shape. The project records `upstream` + `default_branch`
  (`Project.upstream`/`default_branch`, broadcast as `upstream`/
  `defaultBranch`). Two things then hold for every session in it: **(1) it is
  born with a standing rule** (`Project.standing_rule` → claude gets it as
  `--append-system-prompt`, recomputed on every start so handoff/restart
  respawns carry it; codex gets it as an `AGENTS.override.md` written into
  the repo + listed in `.git/info/exclude`, never `.gitignore`) — never
  commit/push the default branch, branch from `upstream/<default>`, push the
  branch to origin, `gh pr create --repo <upstream>`, report the PR link;
  **(2) it never starts stale** — `create_session` runs `_external_sync`
  synchronously before the spawn (`git fetch upstream`; ff-merge the default
  branch only when it's checked out and clean, nudge the fork's copy along;
  bounded by `EXTERNAL_SYNC_TIMEOUT`, `EXTERNAL_SYNC=0` opts out). It lives
  under `projects/` so removal is the gh delete-the-folder contract. Pasting
  the URL of a repo already cloned as `gh` converts it in place. PM verb:
  `external_project`. Test: `python3 test_external_project.py` (mocked gh +
  real temp git repos; nothing leaves the machine).
- **Accounts (subscription routing):** the harness can hold N Claude
  subscription logins at once — each account = a config dir under
  `~/.clawd-accounts/<name>` (`CLAUDE_CONFIG_DIR` isolates the credential
  store; `default` = plain `~/.claude`). Sign-in happens **in the UI**: the
  accounts panel at the foot of the projects rung spawns a claude session
  under the fresh dir and you complete OAuth in its terminal. A poller tracks
  per-account usage via Claude's (undocumented — always degrade) OAuth usage
  endpoint; new sessions spawn under the ACTIVE account, auto-switched to the
  COOL (< `SUB_HOT` 97% — hop at ~3% left; a pool nearing its 5h session
  wall gets no new work) pool whose WEEKLY window resets soonest (use-it-or-lose-it;
  headroom is only the tie-break — see `_route_key`), debounced via `SUB_*`
  env knobs. **Freshness ranks, it doesn't filter** (2026-08-22): readings
  < 3×`USAGE_TTL` are ranked first, but when every fresh pool is hot a
  stale-but-cool one (< `USAGE_STALE_TRUST`, 12h) is routed to instead of a
  fresh-but-walled one — heart spawned onto a 100% plan while its only
  cool pool sat on a 2h-old reading (idle sessions hold the grant, so the
  poller can't renew its token). `_candidates`/`_pick_pool`;
  test: `python3 test_stale_route.py`. **Capacity isn't the only bar: a pool whose plan can't do
  **fable** is skipped by every routing decision at any headroom, and idle
  sessions on it are evacuated** (`SUB_REQUIRE_FABLE`, 2026-08-09 — the slop
  org went Opus-only and the router kept picking it *because* it was the
  emptiest pool on the box). Detection is the usage payload's scoped fable
  window, which a carrying plan advertises from 0% used; it's a heuristic on
  an undocumented endpoint, so it degrades one way only — unknown never
  convicts, the gate never strands the router (`_routable_first` falls back
  to the full roster and logs), and `SUB_FABLE_OK` / `SUB_NO_FABLE` override
  it by hand. Test: `python3 test_fable_gate.py`. Sessions
  record `account`+`config_dir` at spawn so `--resume` finds the right dir —
  but they don't stay pinned: the sweep hands an idle session off to a
  better pool, both as a drain rescue and as a **rebalance** onto the
  reset-soonest pool (`_rebalance_win`, `SUB_REBALANCE*` knobs); and a
  prompt that bounces off a hard-dead plan (the CLI's limit line, no Stop)
  triggers an immediate handoff that **redelivers the bounced prompt**
  (`rescue_bounced_prompt`, `BOUNCE_*` knobs); and the limit banner itself,
  spotted in the PTY stream, triggers an endpoint-confirmed handoff in
  seconds with auto-redeliver / auto-"continue" (`_scan_for_limit` →
  `rescue_limit_wall`, `LIMIT_CONTINUE`) — the never-see-a-rate-limit
  contract in `EXPECTATIONS.md`. That scan catches **two paints** (2026-08-13):
  the classic one-line banner (spaced needle) and the newer
  **extra-usage-credits modal** ("You've reached your <model> limit … uses
  usage credits" — a blocking ink dialog whose Enter could spend real credits
  or switch models, painted when a scoped weekly window like Fable's runs
  dry). ink pads dialogs with cursor motion, so the modal needle matches
  whitespace-stripped text off a raw-byte window, the resume-gate technique.
  Test: `python3 test_limit_modal.py`. The one carve-out: **sign-in ceremony
  sessions** (`ClaudeSession.ceremony` — spawned by the 🧠 panel's add /
  re-sign-in buttons) deliberately sit on a broken account, so every
  rescue/handoff path skips them for the session's lifetime; a re-sign-in
  ceremony also auto-types `/login` once the TUI is up. The gate is
  `_opens_normal_tui` — **onboarding state, not credentials**: any dir that
  has ever been signed in opens claude's normal TUI (which needs `/login`
  typed into it), and a *revoked* login is deleted from the credential store,
  so the old "are the creds present?" test skipped exactly the re-sign-in
  case it existed for. Only a never-signed-in dir (no `.claude.json`) is left
  alone — there the CLI paints its own login/onboarding flow and injected
  keystrokes would garble it. The SessionStart wait is soft (a hook that
  never fires — e.g. a credential-less dir — must not cost the feature): it
  falls through to a fixed timer and types anyway.
  Deep doc: [`docs/fleet/SUB-ROUTING.md`](docs/fleet/SUB-ROUTING.md); what the
  accounts panel should display + mis-bound-login runbook:
  [`docs/fleet/ACCOUNTS-PANEL.md`](docs/fleet/ACCOUNTS-PANEL.md) (key trap: one
  email can hold seats in several orgs — the ORG is the usage pool); probe:
  `python3 tools/usage_probe.py [config_dir]`.
- **Shared kit (`share/`)** (2026-08-16): repo-shipped skills + CLIs that every
  session on every machine must have — today the **todo skill + CLI** for
  Austin's shared list (`todo.atg.link`). `_sync_shared_kit()` runs at boot:
  `share/skills/*` → `~/.claude/skills/` (the `SHARE_PATHS` symlink fans it
  into every account dir; accounts with a *real* skills dir get a direct copy)
  and `share/bin/*` → `~/bin` (0755). Push-to-main distributes it fleet-wide
  because a `server.py` change restarts every box. The repo copy is canonical
  (local edits are overwritten — edit `share/`, not `~/.claude/skills/`). The
  **token never rides git**: it lives in `~/.clawd-todo.env`, placed once per
  machine (ADD-MACHINE.md Step 1c); a kit-but-no-token box warns in the boot
  log. Test: `python3 test_shared_kit.py` (includes a share/-is-credential-free
  tripwire — this repo is public).
- **Self-project:** the harness always injects *itself* as a **pinned** project
  (`SELF_PID="self"`, `path=HERE`, top of the list, never persisted —
  re-injected each boot) so you can open a session and **live-edit the running
  app**. It's the one project whose path is outside `PROJECTS_DIR`.
- **Graceful self-restart** (companion to live-editing): `watch_ui` polls
  `RESTART_FILES` (`server.py`, `.clawd-harness.env` — both read only at boot);
  a change calls `MGR.request_restart(reason)`, which flags `restart_pending`,
  surfaces a banner in every browser, and **waits until nothing is MID-TURN**
  (`restart_blockers`, 2026-08-09 — *not* "all idle": a session `waiting` on an
  interactive prompt is parked on a human and can sit for hours; counting it as
  busy is how a pending restart becomes a permanent one, and it held a routing
  fix off clawd-head for 30+ min while that box kept spawning onto the very plan
  the fix avoids. Genuinely busy turns and `bg` work still block — a turn
  resumes, a background shell doesn't). Three ways it fires: nothing mid-turn,
  the **`restart now`** button on the banner (`{type:"restart", force:true}`;
  the banner names its blockers so the choice is informed), or the
  `RESTART_MAX_WAIT` (20 min) ceiling — code that can never land is its own
  outage. `watch_ui` re-checks on the 1s tick, since a quiet box stops
  broadcasting `sessions` and would never re-evaluate the clock. Then
  `_execute_restart` SIGTERMs the claude children and `os._exit(0)`s —
  launchd (`KeepAlive=true`) respawns us and sessions `--resume`. So an edit to
  the harness never kills an in-flight turn. The browser auto-reloads on the
  `BOOT_ID` change after reconnect. Manual: WS `{type:"restart"}` /
  `{type:"restartCancel"}`.
- **Live-reload of the UI (no manual reload needed):** `watch_ui` *also* polls
  `WATCH_FILES` (`index.html`) and, on an mtime change, broadcasts WS
  `{type:"reload"}` → every open browser calls `location.reload()`
  (`index.html` ~L495). So **saving `index.html` is enough to see the edit —
  tabs on *this* box hard-reload themselves within ~1s**; never tell the user to
  reload manually, and don't restart the server for a UI-only edit (that's only
  for `RESTART_FILES`). Caveat: this needs `server.py` to be running.
  **Enough to *see*, not enough to *ship*** — this reloads only browsers attached
  to this machine's harness, straight off the working-tree file. Every other box,
  and the `h.atg.link` UI the user is actually looking at, is still on the last
  pushed commit. Don't let a working local reload (or a `uiprobe` screenshot,
  same source) stand in for a deploy: finish with `tools/shipcheck.py` (see
  "Definition of done" under Run / test).
- **One WebSocket per browser, multiplexed** — a client subscribes to one session
  (its PTY bytes + transcript); session metadata (titles, busy badges) fans out to
  all clients.
- **Channels:** WRITE = keystrokes → PTY; READ (visual) = raw PTY bytes → xterm.js
  renders the ANSI; READ (structured) = transcript JSONL tailed → slim events. We
  never parse the terminal's "weird text."
- **Two engines** (2026-08-07) — a session is either **`claude`** or **`codex`**
  (`ClaudeSession.engine`, chosen at spawn from the `codex` button beside ＋ new
  session, persisted in the registry, defaulting to claude everywhere it's
  missing). It's cheap *because* of the narrow channel contract above: codex's
  hook payloads use the same field names, so `on_hook` and everything downstream
  of `_slim_event` are engine-blind. All CLI differences live behind the
  **`Engine`** strategy object (`s.eng` → `argv`/`env`/`hook_setup`/
  `transcript_globs`/`slim_event`/`send_settle`/`bg_probe`); a third engine is
  one subclass. **The rule that matters: everything in the subscription router
  is fenced behind `Engine.routes_accounts`** — handoffs, the rebalance sweep,
  both rescues, and both PTY tripwires read *claude's* screens and act by moving
  a session between Anthropic plans, so they must never run on another engine.
  codex is single-login, so `EXPECTATIONS.md` is a claude-only contract — but
  its **plan/usage does show on the 🧠 page** (green card, read-only, "not
  routed"): `codex app-server` answers `account/rateLimits/read` over JSON-RPC
  in ~0.5s, so unlike the rollout scrape this repo first assumed, the number is
  live. Multi-login codex routing is therefore *possible* and simply not built. Full design, what's verified, and
  what still isn't: **[`docs/CODEX-ENGINE.md`](docs/CODEX-ENGINE.md)**.
- **The PM (`controller/`)** — the AI middle manager. It is a **WS client like
  any other** (never imports `server.py`) with one tool surface,
  `controller/verbs.py`, shared by the MCP server and the in-process brain.
  Deep doc: **[`docs/CONTROLLER.md`](docs/CONTROLLER.md)**. The trap to know:
  **a harness feature the verbs don't surface does not exist to the PM**, and one
  the *persona* (`controller/prompts/private.md`) doesn't mention won't get used
  even when the verb exists — codex shipped for a day reachable only from a
  `spawn` argument the persona steered away from. When you add a harness
  feature, ask what the PM should now be able to see or do, and change three
  places together: the verb, its MCP description, and the persona.
  **Pipelines** (2026-08-08) are the multi-step form: one task, ≤6 ordered
  steps, a session and engine each, chained by the autopilot without an LLM turn
  — the shape "research on claude → double-check on codex → claude writes it up"
  needs, since a PM turn ends when it replies. `controller/test_pipeline.py` runs
  that exact chain against the mock harness.
  **Voice** (2026-08-16): the PM tab's **🎙 talk** button opens a live
  speech-to-speech session (OpenAI `gpt-realtime` over WebRTC — semantic VAD,
  barge-in) whose tools run against the same `/pm` endpoints the typed chat
  uses, with `ask_pm` handing hard/write work to the real PM brain. Server half
  is `controller/voice.py` (`POST /api/voice/token` mints an ephemeral secret —
  the real `OPENAI_API_KEY` never leaves the controller; `/api/voice/lore`
  serves the clawd-md knowledge base from `CONTROLLER_LORE_DIR`). Voice needs
  HTTPS or 127.0.0.1 for the mic, so production is `h.atg.link`'s PM tab. Deep
  doc: the "Voice front-end" section of `docs/CONTROLLER.md`; recipe source:
  github.com/clawdbotatg/gpt-voice `INTEGRATION.md`. **Full-duplex on device
  speakers needs OS-level echo cancellation a browser can't reach — the three
  build-out experiments (native iOS shell, native macOS companion, WASM AEC)
  are specced as agent handoff docs in [`docs/voice/`](docs/voice/).**
- **Hooks → turn signal:** injected via `claude --settings <generated>` →
  each hook `curl`s stdin to `POST /hook` → broadcasts `hook` events
  (Stop / UserPromptSubmit / Pre+PostToolUse / SessionStart+End). Drives the
  working/idle pill. **Stop** carries `last_assistant_message`. Stop alone
  overstates idleness: claude can end the turn with background shells
  (`run_in_background`) or background agents still running. `poll_bg` (watch_ui
  sweep) reads the status claude itself publishes to
  `<config_dir>/sessions/<pid>.json` — `"shell"` = bg shell; `"busy"` while our
  Stop said idle = bg agents (`delegatedActive`) — and surfaces it as session
  `status:"background"` + `bg:"shell"|"agent"` (cyan in the UI). Undocumented
  file, so degrade to `""` on any read failure; truly-disowned `nohup` jobs are
  invisible even to claude. bg also vetoes *optional* account moves
  (hot-evacuation/rebalance) so a respawn never kills live background work.
- **Images:** `POST /upload` saves to `.clawd-harness-uploads/`; the path is folded
  into the message and claude `Read`s it (vision works by file path).
- **AI session naming:** optional. Set `BANKR_API_KEY` + `BANKR_BASE_URL` +
  `BANKR_API` (`openai` | `anthropic` | `bankr`). `bankr` = OpenAI-compatible body
  at `https://llm.bankr.bot/v1/chat/completions` authed with an `X-API-Key` header.
  Off → first-prompt titles. Regenerates at prompt 1, then every 3 (3, 6, 9,
  …) via `name_at_prompt()` — naming is cheap + async, so a steady cadence keeps
  a long session's title sharp. Secrets
  load from a gitignored **`.clawd-harness.env`** (`_load_env_file` at boot — the
  launchd daemon doesn't inherit your shell env, so this is the way).
- **📌 pin board + its test hint:** pinning parks a session as "coded, not yet
  verified" — it leaves the tab strip/sessions rung and lives on the board
  (`pin` WS op; pure metadata, the claude keeps running and is promptable from
  the card). Because the *only* thing standing between a pin and done is a
  human going and checking something, the server runs one LLM pass on pin
  (`TEST_SYS_PROMPT` → `generate_test_hint` → `s.test_hint`, broadcast as
  `testHint`) and renders it as the card's **blue line**: one imperative
  instruction for *you* ("open /eq on the god-mode machine during the next show
  and verify composite stays at 30 fps"). Refreshed on each Stop while pinned,
  cleared on unpin, persisted (a restart must not blank the board), backfilled
  at boot for pins that lack one. Same gateway as naming; `TEST_HINT_MODEL`
  overrides `BANKR_MODEL` if this job ever wants a stronger tier.
  **Pinning also compacts** (`_on_pinned` → `_compact_for_pin`): parking is the
  one moment compaction is free — nobody is waiting on the answer — and the
  return trip (you come back days later, after testing) then opens on a full
  context window instead of an auto-compact firing mid-thought. The hint is
  derived FIRST, off the un-thinned transcript, then `/compact` is typed into
  the TUI once the session is genuinely idle (never mid-turn: `busy` races the
  composer, `waiting` would answer a TUI prompt with the literal text
  "/compact"), bounded by `PIN_COMPACT_WAIT`; `PIN_COMPACT=0` opts out.
  **`Engine.compact_cmd` ends in a space and must keep doing so:** typing
  `/compact` leaves the slash-command autocomplete menu open and the menu eats
  the submitting CR — the first cut of this shipped-in-testing as "/compact"
  parked in the composer, picker up, nothing run. The space completes the
  token, the menu closes, the CR submits (`⎿ Not enough messages to compact.`
  is what a *working* one says on a short session). Control sends also take the
  full `SEND_SETTLE`, because what has to finish before the CR is the menu
  closing, not a paste burst. It goes
  through `send_message(..., control=True)` — a harness send, not a human one,
  so it skips the bounce watchdog (a slash command fires no UserPromptSubmit,
  which the detector would read as a walled plan) and leaves `prompted_at`
  alone. The Stop-side hint refresh is gated on `prompt_count` moving, so the
  turn `/compact` itself ends can't overwrite a good hint with one read off a
  summarized transcript.
- **The resume gate — the harness presses Enter for you** (2026-08-09). Claude
  CLI 2.1.226 resumes a session older than 70 min AND over 100k estimated
  tokens (`CLAUDE_CODE_RESUME_THRESHOLD_MINUTES` / `_TOKEN_THRESHOLD`) onto a
  modal offering three numbered options — summarize, resume as-is, stop asking
  — with the first preselected, and then **waits**. In a browser harness nobody
  is there to press Enter, and *every* resume path hits it (daemon restart,
  graceful self-restart, account handoff, every rescue respawn) on exactly the
  long-lived sessions that matter: you'd come back to a session that had been
  "resumed" hours ago and never moved. Worse, a prompt delivered into that
  modal isn't inert — the options are **numbered**, so a message starting `3`
  could pick *Don't ask me again* and its own CR would confirm it.
  So `_scan_for_resume_gate` (third PTY tripwire, beside the limit banner and
  the onboarding screen) answers it the moment it paints — **option 1 runs
  plain `/compact`**, verified in the CLI bundle, so accepting is both the
  unblocking move and the cheap one. You return to a session that already
  compacted itself. `RESUME_GATE=0` opts out; `RESUME_GATE_WINDOW` (120s) sizes
  the arming window. Test: `python3 test_resume_gate.py` (runs against a real
  885-byte capture of the modal's bytes).
  **Since 2026-08-18 the scan is a dormant backstop, not the normal path:**
  "cheap" above meant one CR — the accepted option still runs a full-context
  `/compact` turn, billed to whatever pool the session resumed onto, which at
  handoff-sweep scale was the dominant per-move token cost. So the harness now
  suppresses the modal at the source: every claude child gets
  `CLAUDE_CODE_RESUME_THRESHOLD_MINUTES` pinned huge (the CLI's own age floor
  for painting it, verified in the 2.1.235 bundle), so harness-initiated
  resumes come back as-is, token-free. `RESUME_MODAL_SUPPRESS=0` opts out; an
  operator's own export of the var wins. The knob is undocumented and the
  feature is server-side-flagged, so it degrades one way — if the CLI ignores
  it, the scan still answers exactly as before. Test:
  `python3 test_resume_suppress.py`. (Context: the on-demand routing plan,
  `docs/fleet/ON-DEMAND-SUB-ROUTING-PLAN.md`, removes the idle handoffs
  themselves; this kills the per-move compaction cost independently.)
  Four things here are measured, not assumed, and are the ones to not re-break:
  **(1)** ink pads this dialog with cursor motion, not spaces, so de-ANSI'd text
  arrives *space-free* (`Resumefromsummary…`) — `_flat_pty` strips whitespace on
  both sides, and a spaced needle like `_LIMIT_BANNER_RE`'s matches nothing here;
  **(2)** the scan buffers **raw bytes** and re-strips the window each read
  instead of concatenating per-chunk text like the older two scans — a chunk
  boundary inside an escape sequence leaks junk into the needle and made it miss
  the modal entirely at small chunk sizes; **(3)** there is **no confirming
  oracle** — claude's status file still reads `idle` while the modal is up — so
  unlike `rescue_limit_wall` (which re-confirms against the usage endpoint) the
  needle guards itself, by demanding the whole option list *and* the live footer;
  hence the deliberate rule that **no file in this repo quotes that option list
  verbatim** (a session merely replaying such a file would trip it — the same
  echo trap that respawn-cycled sub2 in 07-16), which `test_resume_gate.py`
  enforces by scanning `server.py` and this file; **(4)** arming is the exact
  mirror of the onboarding scan — **resume-only**, one-shot, and disarmed by any
  write to the PTY so our CR can never land between a harness send's text and its
  own submitting CR.
- **Right model for the right job:** naming is a cheap, frequent, fire-and-forget
  labeler (~900 input tokens, 3×/session, async) — so `BANKR_MODEL` = **`qwen3-coder`**,
  the winner of a full 41-model cost+speed+reliability survey: ~$0.032 per 1,000
  calls (½ the cost of `gemini-3.1-flash-lite`), ~510ms (fastest reliable), 5/5
  clean JSON, and on-domain (a code model naming code sessions). `deepseek-v3.2`
  is an equal runner-up. Three traps the survey exposed: **reasoning models**
  (`gemini-3-flash`, the `-pro`/`gpt-5.4`/`glm`/`kimi` tiers) blow the 120-token
  budget *thinking* and return `null` content; **fast ≠ cheap** (`grok-4.20` was
  quickest but 18× the price); **cheapest ≠ usable** (`gemma-4-*` only emit clean
  JSON 1/5, wrapping it in prose). The future **AI controller** layer is a
  *different* job (reasoning, tool decisions) and should pick its own stronger
  model — likely one of the very reasoning models that are wrong for naming.
- **Re-benchmark the naming model regularly** — new models ship constantly. Run
  **`python3 bench_naming.py`** (no args → pulls the full live model list and
  tests every model on the real naming prompt for JSON-reliability + median
  latency, ranks them, recommends one). If a model clearly beats the incumbent,
  update `BANKR_MODEL` in `.clawd-harness.env`. **Cadence: roughly quarterly —
  last run 2026-06, next ≈2026-09.** The script reuses `server.NAME_SYS_PROMPT`
  and the `.clawd-harness.env` creds, so it never drifts from the app or hardcodes
  a key.
- **Auto-TLDR** (2026-08-16): come back to a wall of text and wish someone had
  tapped "tldr" while you were gone — so the harness does. On Stop, if the
  reply is long (`wants_auto_tldr`: ≥2 real paragraphs or one huge one) and
  **nobody is subscribed** to the session, it sends the chip's prompt itself.
  Armed only by a **browser** send (`via` tag on the `send` frame — controller/
  pipeline prompts carry none, so PM-driven sessions are never injected into),
  consumed at the next Stop (one auto-tldr per human prompt; the tldr turn
  can't re-trigger), skipped for ceremony/pinned sessions, and re-checked after
  a short grace so a viewer arriving at the Stop boundary wins. Logged with
  `via:"auto"` (excluded from chip mining). Knobs: `AUTO_TLDR=0` opts out,
  `AUTO_TLDR_TEXT/MIN/LONG/DELAY`. Test: `python3 test_auto_tldr.py`. Known
  limit: an idle-but-subscribed desktop tab left on the session suppresses it
  (subscription is the "someone's looking" signal; phones drop theirs on lock).
- **Quick-prompt chips:** the one-tap buttons on the session-name line above the
  composer (`QUICK_PROMPTS` array in index.html) — the things the user actually
  says most ("tldr", "okay what is next?", "yes, go", …), ordered most→least
  used (most-used farthest left; hover shows what a chip sends). Every browser
  send is also
  appended to the gitignored `.clawd-harness.prompts.jsonl` (`log_prompt` in
  server.py; `via:"quick"` marks chip taps apart from typed prompts). **Re-mine
  the ranking every few months**: `python3 tools/mine_quick_prompts.py` (reads
  send log + transcript store, ranks candidates), then reorder/extend the array
  by hand — custom chips are welcome, it's just an array edit. Last mined
  2026-08.
- **🔥 Irons** (2026-08-26): a level ABOVE projects — an iron is a **named
  group of projects** ("irons in the fire") for tracking one effort that spans
  repos. **Created with a title only**: the one-line description is AI-derived
  (the stateless `ironDescribe` WS op → `IRON_DESC_MODEL`, default
  `claude-haiku-4.5` on the bankr gateway, falling back to `BANKR_MODEL`) from
  member projects + session titles, requested by whichever browser is looking
  at an iron surface (throttled hourly / on membership change) and stored like
  any other edit, so every device sees the same sentence; tags are edited
  later via the ✎ overlay, never at creation. The 🔥 header icon left of the 🗂️
  projects icon opens `#/irons` (the list). **The list is a PRIORITY order,
  not a date sort** (2026-08-26): a new iron lands at the TOP, cards carry a ⠿
  drag handle (pointer events — works on touch; the handle is the only grab
  surface so tap-to-open and scroll survive) and the dragged order persists —
  fleet: the array order of the `prefs` frame's `irons` field (`clean_irons`
  keeps order); direct: a `rank` field written by the `ironOrder` op, `irons`
  frames arrive rank-sorted. Under the head sits **ONE box** (`#ironfilterrow`),
  the projects-rung deal exactly: a static node (a frame's repaint can't eat
  mid-type text or steal focus), arrival focuses it (desktop), typing narrows
  by title, Esc clears, Enter with ONE match left dives into that iron — the
  "get me into that effort" keyboard path — and the SAME text is the name for
  the ＋ create button beside it (Enter never creates; only the button does).
  **There is no separate create form** — a second create UI shipped briefly on
  2026-08-26 and was explicitly killed; don't bring it back. **An iron has NO page of its own**
  (the intermediate detail page was removed 2026-08-26): tapping an iron —
  from the list, a project card's 🔥 badge, or a `#/i/<id>` deep link — dives
  STRAIGHT into its warmest session (`openIron`), and the whole iron identity
  lives in ONE row (`#ironrow`, painted by `renderIronRow`) between the top
  bar and the session tab strip: ← irons · 🔥 title · AI desc · member
  project chips + ＋ add (the `#ironaddmodal` checklist) · ✎ (edit overlay
  `#ironedit`) · 🗑 (two-tap delete). The strip + swipe/cycle ride
  `railSessions()`, scoped = the iron's WHOLE roster across machines
  including 📌 pinned members (dashed, at the end); tab taps switch the
  terminal in place (open one highlighted). **The scope is entered ONLY through
  irons** (the list / a `#/i/` link) — normal session navigation NEVER
  auto-enters an iron just because the project is a member (an auto-scope cut
  yanked the full strip away mid-work, 2026-08-26 — never again). The row
  rebuilds only on a content-fingerprint change (so the 🗑 arm survives
  session frames) and hides on any normal rung. An EMPTY iron opens the
  add-project picker over the list. Climbing out (🔥 button / swipe /
  Ctrl+Shift+↑) lands on the irons list. Assignment is the 🔥 corner button on
  each project card (modal picker; one iron per project; most projects stay
  loose). Member refs are `projectRows().id` — the **projectKey** in fleet (a
  gh repo groups once across boxes), the **pid** in direct mode. Storage is
  server-side so all devices agree: fleet rides the relay `prefs` frame
  (second field, per-field merge — see `clean_irons` in `fleet/relay.py`);
  direct lives in the harness registry (`iron*` WS ops → `irons` broadcasts,
  membership auto-cleaned when a project vanishes). Pin board untouched.
  Tests: `python3 test_irons.py`, `python3 fleet/test_relay_prefs.py`,
  `cd tools && node ironprobe.mjs`.
- **index.html** — single page. A 4-level swipe stack — **projects → sessions →
  transcript → tty** (`LEVELS`); swipe right climbs out, left dives in. Projects
  page = card list + an add row (name → create repo, git URL → clone). Sessions
  page is scoped to the selected `currentPid`. View switcher (terminal xterm ↔
  transcript bubbles), **key bar** (sends raw escape seqs to drive TUI menus —
  works even on touch where the terminal is read-only), message box
  (type/dictate/paste images). The app opens on the projects rung; a session
  opens as the live terminal on every device (the transcript view was pulled —
  `DEEP_VIEW`). Terminal is **read-only on touch by default** (mobile dictation
  streams self-revising text that xterm forwards as garbled keystrokes) — the
  key bar's **⌨ toggle** opts into direct typing (soft keyboard → PTY) for TUI
  prompts/shells; it resets to read-only on leaving the tty view, and dictation
  should still go through the composer. **Shared-PTY
  sizing:** one PTY can't render two geometries, so resize frames are size
  *claims* and the server follows a single owner — deliberate acts (opening the
  tty view, resizing the window, typing/sending) claim it; reconnect/refit
  maintenance resizes only apply if you already own it; leaving the view or
  going hidden releases it (fallback: most recent remaining viewer). See
  `claim_resize` in server.py + the `resize`/`ttySize` rows in
  `docs/WS-PROTOCOL.md`. Don't regress this to last-resize-wins: it's what
  stopped a background desktop tab from yanking the terminal down to/up from
  phone size mid-use. **The geometry + owner must also survive an in-place
  respawn** (2026-08-20): handoff / onboarding-heal rebuild the session under
  the same cid, and the first cut opened the new PTY at the 120×34 boot
  defaults with no owner — the carried-over phone got a hello at the wrong
  dims and then waited forever for a `ttySize` nothing would send (every line's
  tail wrapped: the "looks broken again" screenshot). `clone_for_respawn`
  carries `tty_cols/rows`, `start()` opens at them, and `adopt_viewers()` is
  the ONE place viewers move across (owner first, then re-subscribe, so the
  hello is already right). Don't reintroduce a hand-rolled carry loop. Test:
  `python3 test_respawn_size.py`. Attaching to a PTY another device sized (`hello` dims ≠
  ours) renders that device's replay mangled — once our claim is applied the
  client auto re-subscribes for a clean replay (`staleGeomReplay` in
  index.html), so no manual reload. The server also drops the ring buffer
  whenever a claim changes the PTY *width* (`_apply_size`): bytes painted for
  another width rewrap into garbage in any replay (the mobile
  scroll-up-shredded-scrollback bug), so replays only reach back to the last
  width change — and a shallow (just-fenced / fresh-boot) replay is preceded by
  a **transcript-rendered seed**: the recent conversation as soft-wrapped plain
  text pushed above the visible screen (`_history_seed_bytes` in server.py,
  `SEED_*` knobs), so scroll-up on a phone shows the session's past instead of
  an empty buffer. Touch-scroll mechanics are verified end-to-end by
  `tools/scrollprobe.mjs` (phone-emulated headless Chromium +
  compositor-synthesized pans; safe — it never subscribes to a session).
- **Repaint, don't rebuild (the projects rung).** A rung repaints on every
  server frame, and `projects` frames are frequent — session state moves a
  couple of times per *tool call*, times the number of machines in fleet mode.
  So `renderProjectRung` may never do `projectsEl.innerHTML = ''`: `#projectlist`
  is itself the scroll container, so emptying it collapses `scrollHeight` and the
  browser clamps `scrollTop` to 0 — the list jerks to the top several times a
  second while anything is working. Instead the header + add row are built **once**
  (`ensureProjectChrome`) and the cards are reconciled by id (`fillProjectCard`
  swaps innards, the node survives). Two corollaries that are easy to
  re-break: **only arrival may focus the filter box** (`setView` →
  `focusProjectFilterOnArrival`; a repaint that focuses it also scrolls it into
  view), and **never refill that input from the `projectFilter` mirror** — text
  the `input` event hasn't mirrored yet is real, and on touch composition and
  dictation live there, so a rebuild eats whole words. `tools/rungprobe.mjs`
  asserts all three. The server half is the memo in `broadcast_projects`: every
  hook bumps `last_active`, so without it a `projects` frame fires per
  Pre/PostToolUse — the fingerprint excludes `lastTouched` (nothing renders it;
  it only feeds the sort, and a reorder still changes the pid order).
- **🔎 tab-strip filter** (2026-08-09) — a small box that **hovers over the far
  right of the `#sessionbar`** and narrows the strip to sessions whose handle /
  title / tldr / live digest / project (and, in fleet, machine) contain what you
  type; space-separated words are ANDed, Esc clears, Enter opens a filter that
  narrowed to one. Purely client-side and purely cosmetic — it hides tab *nodes*,
  touches no session, sends nothing, and the **open session's tab is never
  filtered away** (losing "you are here" is worse than one extra tab). Two
  mechanics carry it: `position:sticky; right:0` (not absolute — `#sessionbar`
  *is* the scroll container, so an absolute child would scroll away with the
  tabs) plus a left-fading `--panel` gradient so tabs vanish behind it; and the
  same **repaint, don't rebuild** rule as above — `renderSessionBar` no longer
  does `innerHTML=''`, it wipes the tabs *around* the box (`ensureTabFilter`) and
  `insertBefore`s new ones, because the box is a live `<input>` and even
  re-`appendChild`ing it blurs it mid-word. `tools/tabfilterprobe.mjs` guards it.
  On touch it collapses to the 🔎 alone until tapped (the strip is the whole
  screen there). PM threads ride the same strip and get no filter.
- **URL routing** — nav state lives in the **hash** (the `?t=` token stays in the
  query): `#/` projects · `#/p/<pid>` sessions · `#/p/<pid>/s/<cid>` transcript ·
  `…/tty` terminal. So a reload (or a shared link) lands back on the same
  project/session/depth, and back/forward work. `setView`/`subscribe` write it via
  `syncUrl()`; on boot `parseHash()` seeds `pendingNav`, which `resolvePendingNav()`
  applies once the server's project then session snapshots arrive (gracefully
  falling back if the named project/session is gone). `syncUrl()` no-ops while a
  restore is pending; a `lastWrittenHash` guard keeps our own writes from
  re-triggering the `hashchange` handler. Creating a session switches you into it
  (`pendingNewFocus` → the server's `focus` reply opens the new `cid`). **Building
  a link into a specific session (incl. the fleet `#/m/<machine>/p/<projectKey>/s/<cid>`
  form + how notifications construct them): see [`docs/DEEPLINKS.md`](docs/DEEPLINKS.md).**

## Three non-obvious gotchas (baked into server.py — don't regress)
1. **`SCRUB_ENV`** — scrub `CLAUDECODE` / `CLAUDE_CODE_*` / `ANTHROPIC_API_KEY`
   etc. from the child env, or a nested `claude` runs in embedded mode (no
   transcript written) and bills metered API instead of the subscription.
2. **`SEND_SETTLE` + bracketed paste** — pause between typing text and the
   `\r`, or claude's TUI treats `text`+`\r` as a paste and the `\r` doesn't
   submit. Short messages use `SEND_SETTLE_MIN` (~0.7s); big/multi-line use
   `SEND_SETTLE` (~1.5s). And the text itself must ride as a **bracketed
   paste** (`ESC[200~ … ESC[201~`, `Engine.bracketed_paste`, 2026-08-26):
   claude's TUI **drops the head of a large unbracketed keystroke burst** — a
   1317-char dictated prompt arrived as its last 295 chars, cut mid-word, and
   read like the harness had "summarized" the message. Measured A/B on a real
   TUI (raw 295/1317 vs bracketed 1317/1317; the kernel PTY layer throttles
   fine and was exonerated). Control sends (`/compact `) stay raw on purpose —
   the slash-command menu only reacts to typing. Per-engine flag; codex is
   opted out until verified the same way. **Re-verified 2026-08-26 against
   idle, mid-turn (streaming) and mid-tool (Bash running) TUIs, single-line
   and multi-line: delivery is intact in all of them.** What still LOOKS like
   truncation: the TUI echoes only the tail of a long paste, and a mid-turn
   send rides as steering with no echo at all — a display problem, answered by
   the 🕘 sent history (`tools/sentlogprobe.mjs` above). Before re-opening a
   "my text got cut off" report as a delivery bug, diff
   `.clawd-harness.prompts.jsonl` (what the harness got) against what claude
   *did* — on 08-26 the "lost" message had been received in full and acted on.
3. **`CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1`** in the child env — Claude Code
   can render its TUI in the terminal's *alternate screen* (a server-side
   rollout: it flips on per-account with no CLI update or harness change).
   xterm.js has **no scrollback in the alt buffer**, so the ring replay and
   `_history_seed_bytes` paint into the hidden normal buffer and mobile
   scroll-up finds nothing — the whole scrollback contract dies silently.
   Diagnose with an unmatched `\x1b[?1049h` in a session's replay bytes.

## Known issues / next
- **Transcript tailer logs `tailing …` repeatedly** (busy-reattach loop, inherited
  from console) — worth fixing.
- **codex's turn signal dies if two harnesses share a box.** Hooks *do* fire
  (verified end-to-end on clawd-head — the old "unproven" note here was stale;
  see "What is verified" in [`docs/CODEX-ENGINE.md`](docs/CODEX-ENGINE.md)), but
  codex has no per-invocation hook flag, so `_ensure_codex_hooks` writes our port
  into the single shared `$CODEX_HOME/hooks.json`. This repo deliberately
  coexists with clawd-console, so the loser's codex sessions POST to a dead port
  and go silent — a working terminal with inert badges. Fix if it bites: a
  per-harness `CODEX_HOME` (costs a `codex login` each) or fan the hook command
  out to both ports. Still open: scrollback (gap 1) and codex `send_settle`
  timing — "Phase 1.5"/"Phase 2" in that doc.
- Roadmap (the reason for the fork): the **AI controller** layer, Telegram
  front-end. Multi-session, multi-project (the projects layer), view switcher,
  and AI naming already exist. ("Per-client terminal sizing" was resolved as
  the size-ownership policy above — true per-viewer rendering would need a
  server-side terminal emulator per client and isn't planned.)

## Conventions
- **Never commit** runtime/secret files (gitignored): `.clawd-harness.token`,
  `.clawd-harness.session`, `.clawd-harness.sessions.json`,
  `.clawd-harness.hooks*.json`, `.clawd-harness-uploads/`, `projects/` (the
  cloned repos). Scan diffs for leaked secrets before committing (a gitleaks
  pre-commit hook also runs).
- Git identity here (under `~/clawd/`): **clawdbotatg** /
  `clawd@buidlguidl.com`, over **HTTPS**. Remote: `clawdbotatg/clawd-harness`.
- **Browser stale-cache:** a prior app on a port leaves a cached page on
  `127.0.0.1:<port>`. Hard-refresh (Cmd+Shift+R) or use the LAN URL. The server
  sends `Cache-Control: no-store` on the served UI.
