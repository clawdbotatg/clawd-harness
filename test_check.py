#!/usr/bin/env python3
"""test_check.py — 🔍 double-check: the OTHER engine reviews a session's work.

One tap: the source session is armed and asked for a short brief (a LOCAL
per-review REVIEW-<stamp>.md, git-excluded like HANDOFF.md); the first Stop
AFTER that brief was submitted spawns a reviewer in the same project (codex
for claude, claude for codex) with the brief + the git range since the source
spawned. The brief is claims, the diff is truth, the reviewer never edits. Its
first Stop appends the verdict to the same file and prompts the SOURCE to act
on it. Guards, on a sandboxed copy of server.py and fakes — never the live
daemon:
  1. _git_head: a sha for a repo with commits, "" otherwise;
  2. the review file is local + unique: review_file_name() matches
     REVIEW_GLOB, _exclude_handoff(path, REVIEW_GLOB) writes the anchored
     pattern (idempotent, HANDOFF's entry untouched) and _worktree_dirty
     tolerates any untracked REVIEW-*.md;
  3. head_at_spawn + check_of + check_file + check_pending are ctor params +
     registry fields, kept by clone_for_respawn (landmine 5);
  4. the arm fires on the first Stop PAST the arm-time prompt count — a Stop
     of the turn that was already running when the tap landed is ignored —
     exactly once, and the TTL disarms without spawning;
  5. manager.check refuses: unknown cid, a sign-in ceremony, no conversation
     yet, codex not signed in; on success it arms, excludes REVIEW-*.md, types
     the brief prompt (naming ITS file, "do NOT commit"), reviewer = the other
     engine, and a mid-turn source is armed the same (the send queues);
  6. check_range: base == HEAD → "working tree"; base resolves → the
     diff range; a stale/absent base → the commits-since fallback;
  7. check_spawn: create_session(engine=<reviewer>, title 🔍…, check_of=src),
     waits for the TUI, types the review prompt (brief file, CLAIMS, no
     edits, range); a refused spawn is a log line, never a raise;
  8. meta carries checkArmed/checkOf;
  9. the hand-back: the reviewer's first Stop past its prompt fires
     check_back once (durable flag cleared, registry saved); check_back
     appends "## Review by <engine>" + the verdict to the review file and
     types CHECK_ACT_PROMPT (the file, "think critically", fix/why-not, no
     commit of the file) into the SOURCE via 'check'; an empty Stop payload
     falls back to the transcript; a gone source leaves the findings in the
     file with a note on the reviewer's tab, nothing typed, no raise.
Exits non-zero on any failure.
"""
import sys, types, shutil, tempfile, os, time, subprocess, importlib.util   # (one line: gitleaks bip39)
from pathlib import Path

REPO = Path(__file__).resolve().parent
FAILS = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def load_server_sandboxed():
    tmp = Path(tempfile.mkdtemp(prefix="check-test-"))
    shutil.copy(REPO / "server.py", tmp / "server.py")
    spec = importlib.util.spec_from_file_location("server_sandbox_check", tmp / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_sandbox_check"] = mod
    spec.loader.exec_module(mod)     # HERE = tmp dir → empty registry, inert
    return mod


srv = load_server_sandboxed()
TMP = tempfile.mkdtemp(prefix="checktest-")
srv.PROMPTS_LOG = type(srv.PROMPTS_LOG)(os.path.join(TMP, "prompts.jsonl"))   # never the real log
srv.SUB_ROUTE_ON_PROMPT = False


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, check=True).stdout.strip()


repo = os.path.join(TMP, "repo"); os.makedirs(repo)
subprocess.run(["git", "init", "-q", repo], check=True)
git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")

# --- 1. _git_head ---------------------------------------------------------------
check("_git_head: unborn branch → ''", srv._git_head(repo) == "")
open(os.path.join(repo, "a.txt"), "w").write("1")
git(repo, "add", "."); git(repo, "commit", "-qm", "one")
sha1 = git(repo, "rev-parse", "HEAD")
check("_git_head: the HEAD sha", srv._git_head(repo) == sha1 and len(sha1) == 40)
check("_git_head: not a repo → ''", srv._git_head(TMP) == "" and srv._git_head("") == "")

# --- 2. the review file is local + unique -----------------------------------------
import fnmatch
RF = srv.review_file_name()
check("review_file_name matches REVIEW_GLOB, one per second, sortable",
      fnmatch.fnmatch(RF, srv.REVIEW_GLOB) and RF.startswith("REVIEW-20")
      and srv.review_file_name(1000) != srv.review_file_name(2000), RF)
open(os.path.join(repo, RF), "w").write("# brief")
open(os.path.join(repo, "REVIEW-20260101-000000.md"), "w").write("# older")
open(os.path.join(repo, "notes.md"), "w").write("x")
d = srv._worktree_dirty(repo)
check("_worktree_dirty tolerates untracked REVIEW-*.md files (other untracked still counts)",
      "REVIEW" not in d and "notes.md" in d, d)
check("_exclude_handoff(path, REVIEW_GLOB) → True", srv._exclude_handoff(repo, srv.REVIEW_GLOB) is True)
excl = open(os.path.join(repo, ".git", "info", "exclude")).read()
check("…as an anchored /REVIEW-*.md line", "/REVIEW-*.md\n" in excl)
check("…idempotent", srv._exclude_handoff(repo, srv.REVIEW_GLOB) is True
      and open(os.path.join(repo, ".git", "info", "exclude")).read() == excl)
check("…the HANDOFF-*.md entry still works alongside", srv._exclude_handoff(repo) is True
      and "/HANDOFF-*.md\n" in open(os.path.join(repo, ".git", "info", "exclude")).read())
porc = git(repo, "status", "--porcelain")
check("git status no longer lists any REVIEW-*.md", "REVIEW" not in porc and "notes.md" in porc, porc)
check("…and never touches .gitignore", not os.path.exists(os.path.join(repo, ".gitignore")))

# --- 3. persistence ---------------------------------------------------------------
mgr0 = types.SimpleNamespace(save_registry=lambda: None)
s0 = srv.ClaudeSession(mgr0, cid="c1", session_id="x", resuming=False, pid="p1",
                       head_at_spawn=sha1, check_of="src-cid", check_file=RF, check_pending=True)
check("ctor stores head_at_spawn + check_of + check_file + check_pending",
      s0.head_at_spawn == sha1 and s0.check_of == "src-cid" and s0.check_file == RF and s0.check_pending is True)
row = s0.to_registry()
check("registry row carries all four", row.get("head_at_spawn") == sha1 and row.get("check_of") == "src-cid"
      and row.get("check_file") == RF and row.get("check_pending") is True)
s_def = srv.ClaudeSession(mgr0, cid="c2", session_id="x", resuming=False)
check("defaults are empty", s_def.head_at_spawn == "" and s_def.check_of == "" and s_def.check_file == ""
      and s_def.check_pending is False)
cl = s0.clone_for_respawn()
check("clone_for_respawn keeps all four", cl.head_at_spawn == sha1 and cl.check_of == "src-cid"
      and cl.check_file == RF and cl.check_pending is True)
check("volatile arm starts clear", not s0.check_armed() and s0.check_after == 0 and s0.check_engine == ""
      and s0.check_brief == "")


# --- fakes for the manager half ---------------------------------------------------
class FakeMgr:
    check = srv.SessionManager.check
    check_cancel = srv.SessionManager.check_cancel
    check_range = srv.SessionManager.check_range
    check_prompt = srv.SessionManager.check_prompt
    check_spawn = srv.SessionManager.check_spawn
    check_back = srv.SessionManager.check_back

    def __init__(self):
        self.sessions = {}
        self.projects = {"p1": types.SimpleNamespace(name="proj", path=repo)}
        self.sent = []
        self.spawned = []
        self.spawn = None
        self.broadcasts = 0
        self.saves = 0

    def save_registry(self):
        self.saves += 1

    def send_prompt(self, cid, text, via="", control=False):
        self.sent.append((cid, text, via))
        return True

    def broadcast_sessions(self):
        self.broadcasts += 1

    def create_session(self, pid, **kw):
        self.spawned.append((pid, kw))
        return self.spawn


class FakeSession:
    check_armed = srv.ClaudeSession.check_armed
    check_arm = srv.ClaudeSession.check_arm
    check_cancel = srv.ClaudeSession.check_cancel
    _check_on_stop = srv.ClaudeSession._check_on_stop
    _check_back_on_stop = srv.ClaudeSession._check_back_on_stop

    def __init__(self, mgr, cid, engine="claude", ceremony=False, conv=True,
                 head="", workdir=repo, busy=False, check_of="", check_file="",
                 check_pending=False):
        self.manager, self.cid = mgr, cid
        self.engine, self.pid, self.session_id = engine, "p1", "sid-" + cid
        self.title, self.desc = "T " + cid, ""
        self.prompt_count, self.created = 3, time.time() - 60
        self.ceremony, self.busy, self._conv = ceremony, busy, conv
        self.head_at_spawn, self.check_of = head, check_of
        self.check_file, self.check_pending = check_file, check_pending
        self.check_armed_at, self.check_after, self.check_engine = 0.0, 0, ""
        self.check_brief = ""
        self._wd = workdir
        self.ready = True
        self.alive = True
        self.transcript_text = ""
        mgr.sessions[cid] = self

    def _last_assistant_text(self):
        return self.transcript_text

    def workdir(self):
        return self._wd

    def _fallback_title(self):
        return "fb"

    def _has_conversation(self):
        return self._conv

    def wait_ready(self, timeout=0):
        return self.ready


def wait_for(pred, secs=2.0):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


# --- 4. the arm fires on the first Stop PAST the brief ------------------------------
m = FakeMgr()
spawns = []
m.check_spawn = lambda src, engine, file="": spawns.append((src.cid, engine, file))
a = FakeSession(m, "a")
a.check_arm("codex")
check("arm → checkArmed, remembers the prompt count + engine + a fresh review file",
      a.check_armed() and a.check_after == 3 and a.check_engine == "codex"
      and fnmatch.fnmatch(a.check_brief, srv.REVIEW_GLOB), a.check_brief)
a_file = a.check_brief
a._check_on_stop()
check("a Stop with no new prompt (the turn already running at the tap) is ignored — still armed",
      spawns == [] and a.check_armed())
a.prompt_count = 4
a._check_on_stop()
check("the first Stop past the brief spawns the reviewer once, with the chosen engine + the file",
      wait_for(lambda: spawns == [("a", "codex", a_file)]), str(spawns))
check("…and disarms", not a.check_armed() and a.check_engine == "" and a.check_brief == "")
a.prompt_count = 5
a._check_on_stop()
time.sleep(0.1)
check("a later Stop spawns nothing (one tap = one reviewer)", spawns == [("a", "codex", a_file)])
t = FakeSession(m, "t"); t.check_arm("codex"); t.check_armed_at = time.time() - srv.CHECK_TTL_S - 1
t.prompt_count = 9
t._check_on_stop()
time.sleep(0.1)
check("TTL lapsed → the Stop disarms without spawning", len(spawns) == 1 and not t.check_armed_at)

# --- 5. manager.check gates + success -----------------------------------------------
m5 = FakeMgr()
srv._codex_signed_in = lambda: True
check("unknown cid → reason", m5.check("nope") == "no such session")
c = FakeSession(m5, "c", ceremony=True)
check("ceremony → refused", "sign-in" in m5.check("c") and not c.check_armed())
e = FakeSession(m5, "e", conv=False)
check("no conversation → refused", "nothing to check" in m5.check("e") and not e.check_armed())
srv._codex_signed_in = lambda: False
g = FakeSession(m5, "g")
check("codex not signed in → refused, nothing typed", "codex login" in m5.check("g") and not m5.sent)
srv._codex_signed_in = lambda: True
shutil.rmtree(os.path.join(repo, ".git", "info"))
why = m5.check("g", via="quick")
check("a live claude session arms (reviewer = codex)", why == "" and g.check_armed() and g.check_engine == "codex", why)
check("…REVIEW-*.md excluded in the source's checkout",
      "/REVIEW-*.md\n" in open(os.path.join(repo, ".git", "info", "exclude")).read())
check("…the brief prompt typed into the source, naming ITS review file, via preserved",
      len(m5.sent) == 1 and m5.sent[0][0] == "g" and m5.sent[0][2] == "quick"
      and g.check_brief in m5.sent[0][1] and "do NOT commit" in m5.sent[0][1] and "codex" in m5.sent[0][1],
      str(m5.sent))
check("…broadcast so the badge lands", m5.broadcasts >= 1)
x = FakeSession(m5, "x", engine="codex")
check("a codex source gets claude as the reviewer", m5.check("x") == "" and x.check_engine == "claude")
b = FakeSession(m5, "b", busy=True)
check("a mid-turn source is armed the same (the brief queues in its composer)",
      m5.check("b") == "" and b.check_armed() and b.check_after == 3)
m5.check_cancel("b")
check("check_cancel disarms", not b.check_armed())

# --- 6. check_range ----------------------------------------------------------------
m6 = FakeMgr()
r0 = FakeSession(m6, "r0", head=sha1)
check("base == HEAD → the work is in the working tree", "working tree" in m6.check_range(r0), m6.check_range(r0))
open(os.path.join(repo, "a.txt"), "w").write("2"); git(repo, "commit", "-qam", "two")
rng = m6.check_range(r0)
check("base resolves and differs → git diff <base>..HEAD", f"git diff {sha1[:12]}..HEAD" in rng and "git log --oneline" in rng, rng)
r1 = FakeSession(m6, "r1", head="deadbeef" * 5)
check("stale base → the commits-since fallback", "commits since it started at" in m6.check_range(r1), m6.check_range(r1))
r2 = FakeSession(m6, "r2", head="")
check("no base (pre-feature row) → the commits-since fallback", "git log --since" in m6.check_range(r2))
r3 = FakeSession(m6, "r3", head=sha1, workdir=TMP)
check("not a repo → the fallback, no raise", "commits since" in m6.check_range(r3))
p = m6.check_prompt(r0, "codex", "REVIEW-x.md")
check("the review prompt: brief file, CLAIMS, no edits, the range, the source named, final message = the verdict",
      all(k in p for k in ("REVIEW-x.md", "CLAIMS", "Do NOT edit", sha1[:12], "T r0", "claude", "FINAL message")), p)

# --- 7. check_spawn ---------------------------------------------------------------
m7 = FakeMgr()
src = FakeSession(m7, "src", head=sha1)
rev = FakeSession(m7, "rev", engine="codex")
m7.spawn = rev
out = m7.check_spawn(src, "codex", "REVIEW-y.md")
check("create_session called in the source's project with the reviewer engine + check_of + check_file",
      len(m7.spawned) == 1 and m7.spawned[0][0] == "p1" and m7.spawned[0][1].get("engine") == "codex"
      and m7.spawned[0][1].get("check_of") == "src" and m7.spawned[0][1].get("check_file") == "REVIEW-y.md",
      str(m7.spawned))
check("…title seeded with 🔍 + the source title", m7.spawned[0][1].get("title", "").startswith("\U0001f50d T src"))
check("…returns the reviewer, described", out is rev and rev.desc.startswith("double-checking T src"), rev.desc)
check("…the review prompt typed into the REVIEWER via 'check', naming the file",
      len(m7.sent) == 1 and m7.sent[0][0] == "rev" and m7.sent[0][2] == "check" and "CLAIMS" in m7.sent[0][1]
      and "REVIEW-y.md" in m7.sent[0][1], str(m7.sent))
m7.spawn = None
check("a refused spawn → None, nothing typed, no raise", m7.check_spawn(src, "codex") is None and len(m7.sent) == 1)
rev.ready = False; m7.spawn = rev
check("a reviewer whose TUI never signals ready is still briefed (bounded wait)",
      m7.check_spawn(src, "codex") is rev and len(m7.sent) == 2)
check("no file given → a fresh REVIEW-<stamp>.md", fnmatch.fnmatch(m7.spawned[-1][1].get("check_file", ""), srv.REVIEW_GLOB))

# --- 8. meta ----------------------------------------------------------------------
s0.wrap_closing = False
meta = srv.ClaudeSession.meta(s0)
check("sessions meta carries checkOf + checkArmed",
      meta is not None and meta.get("checkOf") == "src-cid" and meta.get("checkArmed") is False, str(meta and {k: meta.get(k) for k in ("checkOf", "checkArmed")}))

# --- 9. the hand-back: the reviewer's verdict goes to the source ------------------
m9 = FakeMgr()
src9 = FakeSession(m9, "src9")
open(os.path.join(repo, "REVIEW-z.md"), "w").write("# brief\nclaims\n")
rev9 = FakeSession(m9, "rev9", engine="codex", check_of="src9", check_file="REVIEW-z.md", check_pending=True)
backs = []
m9.check_back = lambda rev, last="": backs.append((rev.cid, last))
rev9.prompt_count = 0
rev9._check_back_on_stop("early")
check("a Stop before any prompt landed is not the reviewer's — still pending", backs == [] and rev9.check_pending)
rev9.prompt_count = 1
rev9._check_back_on_stop("VERDICT")
check("the first Stop past the review prompt fires check_back once with the last message; flag cleared + saved",
      wait_for(lambda: backs == [("rev9", "VERDICT")]) and rev9.check_pending is False and m9.saves == 1, str(backs))
rev9.prompt_count = 2
rev9._check_back_on_stop("again")
time.sleep(0.1)
check("later Stops do nothing (one review = one hand-back)", backs == [("rev9", "VERDICT")])
plain = FakeSession(m9, "plain"); plain.prompt_count = 4
plain._check_back_on_stop("x"); time.sleep(0.05)
check("a non-reviewer session never hands back", backs == [("rev9", "VERDICT")])

m9.check_back = srv.SessionManager.check_back.__get__(m9)
ok = m9.check_back(rev9, "1. [should-fix] a.txt:1 — off by one\n\nTLDR: mostly fine")
body = open(os.path.join(repo, "REVIEW-z.md")).read()
check("check_back appends '## Review by codex' + the verdict to the review file (brief kept)",
      ok is True and body.startswith("# brief") and "## Review by codex" in body and "off by one" in body, body)
check("…and types CHECK_ACT_PROMPT into the SOURCE via 'check': the file, think critically, fix or why not, no commit",
      len(m9.sent) == 1 and m9.sent[0][0] == "src9" and m9.sent[0][2] == "check"
      and all(k in m9.sent[0][1] for k in ("REVIEW-z.md", "codex", "think", "critically", "why not", "do NOT commit")),
      str(m9.sent))
check("…the reviewer's tab says where the findings went", "sent back to T src9" in rev9.desc and m9.broadcasts >= 1, rev9.desc)
rev9.transcript_text = "FROM TRANSCRIPT"
m9.check_back(rev9, "")
body = open(os.path.join(repo, "REVIEW-z.md")).read()
check("an empty Stop payload falls back to the transcript's last assistant message", "FROM TRANSCRIPT" in body)
gone = FakeSession(m9, "gone", engine="codex", check_of="nobody", check_file="REVIEW-g.md", check_pending=True)
n_sent = len(m9.sent)
check("a gone source → findings written, nothing typed, a note on the reviewer, False, no raise",
      m9.check_back(gone, "v") is False and len(m9.sent) == n_sent
      and "gone" in gone.desc and "v" in open(os.path.join(repo, "REVIEW-g.md")).read(), gone.desc)
dead = FakeSession(m9, "dead"); dead.alive = False
dr = FakeSession(m9, "dr", engine="codex", check_of="dead", check_file="REVIEW-d.md", check_pending=True)
check("a dead source (not alive) is treated as gone", m9.check_back(dr, "v") is False and len(m9.sent) == n_sent)
srcp = FakeSession(m9, "srcp"); srcp.title = ""
rp = FakeSession(m9, "rp", engine="codex", check_of="srcp", check_file="", check_pending=True)
check("no file on the reviewer row (pre-feature) → a fresh REVIEW-<stamp>.md, still handed back",
      m9.check_back(rp, "v") is True and fnmatch.fnmatch(m9.sent[-1][1].split(" at the repo root")[0].split()[-1], srv.REVIEW_GLOB),
      m9.sent[-1][1][:120])

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}"); [print("  -", f) for f in FAILS]; sys.exit(1)
print("all good — 🔍 double-check: local REVIEW-<stamp>.md, arm fires past the brief, gates, range, spawn, hand-back")
