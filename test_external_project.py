#!/usr/bin/env python3
"""External projects (kind="external"): fork-or-clone someone else's GitHub
repo, sync its default branch from upstream at every spawn, and make every
session in it work branch-and-PR — never a push to the default branch.

Pins the parts that would fail silently:

  * the fork-or-clone DECISION comes from `gh repo view`'s viewerPermission
    (READ → `gh repo fork … --clone`; WRITE/ADMIN → `git clone`), and the
    project ends up ready with an `upstream` remote, upstream URL and default
    branch recorded either way,
  * a failed gh call lands the card on error (with the reason), never ready,
  * the standing rule names the real upstream slug, the fork head, the
    default branch, the PR command and the report-the-link demand — and is
    "" for every other kind and for ceremony sessions,
  * claude's argv carries it as --append-system-prompt; a non-external
    project's argv does not,
  * `_external_sync` against REAL temp git repos: fast-forwards a clean
    default branch to upstream, leaves a checked-out feature branch alone
    (but still fetches, so upstream/<default> is fresh), leaves a dirty
    default branch alone, and never fails the spawn,
  * the codex AGENTS.override.md is written + excluded via .git/info/exclude
    (not .gitignore), prefixes the repo's own AGENTS.md, and refuses to
    overwrite one the repo tracks,
  * the registry round-trips upstream/default_branch.

Nothing leaves the machine: `gh` is monkeypatched, git runs on file:// repos
under a temp dir, and no SessionManager loop or claude is started.

    python3 test_external_project.py
"""
import os
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

import server

FAILS = []


def check(name, ok, detail=""):
    print(f"  {'ok' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def git(path, *args):
    r = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {path}: {r.stderr}")
    return r.stdout.strip()


def make_repo(path, branch="main"):
    os.makedirs(path)
    git(path, "init", "-q", "-b", branch)
    git(path, "config", "user.email", "t@t")
    git(path, "config", "user.name", "t")
    Path(path, "README.md").write_text("hello\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


class FakeMgr:
    """Just enough SessionManager for Project/ClaudeSession lookups."""
    def __init__(self):
        self.projects = {}
        self.lock = __import__("threading").Lock()
        self.saved = 0
        self.broadcasts = 0

    def save_registry(self):
        self.saved += 1

    def broadcast_projects(self):
        self.broadcasts += 1


def main():
    tmp = tempfile.mkdtemp(prefix="clawd-ext-")
    orig_projects_dir = server.PROJECTS_DIR
    server.PROJECTS_DIR = Path(tmp) / "projects"
    server.PROJECTS_DIR.mkdir()
    try:
        run(tmp)
    finally:
        server.PROJECTS_DIR = orig_projects_dir
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("all ok")


def run(tmp):
    # ── the standing rule ────────────────────────────────────────────────
    print("standing rule")
    fork = server.Project(pid="x", name="Hello-World", path="/p",
                          repo_url="https://github.com/clawdbotatg/Hello-World.git",
                          kind="external", upstream="https://github.com/octocat/Hello-World",
                          default_branch="master")
    rule = fork.standing_rule()
    check("names upstream slug", "octocat/Hello-World" in rule)
    check("names fork head", "clawdbotatg:<branch>" in rule)
    check("names default branch", "`master`" in rule and "upstream/master" in rule)
    check("pr command against upstream",
          "gh pr create --repo octocat/Hello-World --base master" in rule)
    check("demands the PR link", "REPORT THE PR LINK" in rule)
    check("forbids pushing default", "NEVER commit to or push `master`" in rule)
    direct = server.Project(pid="y", name="clawd-harness", path="/q",
                            repo_url="https://github.com/clawdbotatg/clawd-harness",
                            kind="external", upstream="https://github.com/clawdbotatg/clawd-harness",
                            default_branch="main")
    drule = direct.standing_rule()
    check("push-access clone: head is bare branch", "--head <branch>" in drule
          and "push access" in drule)
    check("gh project has no rule",
          server.Project(pid="z", name="n", path="/r", kind="gh").standing_rule() == "")
    check("local project has no rule and no upstream",
          server.Project(pid="w", name="n", path="/r", kind="local",
                         upstream="https://x").standing_rule() == ""
          and server.Project(pid="w", name="n", path="/r", kind="local",
                             upstream="https://x").upstream == "")
    check("unknown kind falls back to gh",
          server.Project(pid="v", name="n", path="/r", kind="bogus").kind == "gh")

    # ── argv injection (claude) ──────────────────────────────────────────
    print("argv injection")
    mgr = FakeMgr()
    mgr.projects[fork.pid] = fork
    mgr.projects["g"] = server.Project(pid="g", name="plain", path="/g", kind="gh")
    s = server.ClaudeSession(mgr, cid="c1", pid=fork.pid, session_id="sid",
                             resuming=False, created=time.time())
    s.settings_path = "/dev/null"
    argv = server.ENGINES["claude"].argv(s)
    check("--append-system-prompt present", "--append-system-prompt" in argv)
    check("…carrying the rule",
          "--append-system-prompt" in argv
          and argv[argv.index("--append-system-prompt") + 1] == rule)
    s2 = server.ClaudeSession(mgr, cid="c2", pid="g", session_id="sid2",
                              resuming=False, created=time.time())
    s2.settings_path = "/dev/null"
    check("gh project argv untouched",
          "--append-system-prompt" not in server.ENGINES["claude"].argv(s2))
    s3 = server.ClaudeSession(mgr, cid="c3", pid=fork.pid, session_id="sid3",
                              resuming=True, created=time.time(), ceremony=True)
    s3.settings_path = "/dev/null"
    check("ceremony session exempt", s3.standing_rule() == "")
    s4 = server.ClaudeSession(mgr, cid="c4", pid=fork.pid, session_id="sid4",
                              resuming=True, created=time.time())
    s4.settings_path = "/dev/null"
    check("resume spawn carries it too",
          "--append-system-prompt" in server.ENGINES["claude"].argv(s4))

    # ── registry round-trip ──────────────────────────────────────────────
    print("registry")
    reg = fork.to_registry()
    back = server.Project(pid=reg["pid"], name=reg["name"], path=reg["path"],
                          repo_url=reg["repo_url"], kind=reg["kind"],
                          upstream=reg["upstream"], default_branch=reg["default_branch"])
    check("upstream + default_branch survive",
          back.upstream == fork.upstream and back.default_branch == "master"
          and back.kind == "external")
    m = fork.meta()
    check("meta broadcasts upstream/defaultBranch",
          m["upstream"] == fork.upstream and m["defaultBranch"] == "master")

    # ── sync against real repos ──────────────────────────────────────────
    print("upstream sync (real temp git repos)")
    up = make_repo(os.path.join(tmp, "upstream"))
    origin = os.path.join(tmp, "origin.git")
    git(tmp, "clone", "-q", "--bare", up, origin)
    work = os.path.join(tmp, "work")
    git(tmp, "clone", "-q", origin, work)
    git(work, "config", "user.email", "t@t")
    git(work, "config", "user.name", "t")
    git(work, "remote", "add", "upstream", up)
    # upstream moves on
    Path(up, "NEW.md").write_text("new\n")
    git(up, "add", "-A")
    git(up, "commit", "-q", "-m", "upstream moved")
    up_head = git(up, "rev-parse", "HEAD")
    msg = server._external_sync(work, "main", "work")
    check("clean main fast-forwarded", git(work, "rev-parse", "HEAD") == up_head, msg)
    check("fork's main nudged along",
          git(origin, "rev-parse", "main") == up_head)
    # feature branch checked out: fetch only, branch untouched
    git(work, "switch", "-q", "-c", "feat")
    Path(work, "F.md").write_text("f\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "feat")
    feat_head = git(work, "rev-parse", "HEAD")
    Path(up, "NEW2.md").write_text("new2\n")
    git(up, "add", "-A")
    git(up, "commit", "-q", "-m", "upstream moved again")
    up_head2 = git(up, "rev-parse", "HEAD")
    msg = server._external_sync(work, "main", "work")
    check("feature branch left alone", git(work, "rev-parse", "HEAD") == feat_head, msg)
    check("…but upstream/main is fresh",
          git(work, "rev-parse", "upstream/main") == up_head2)
    check("…and local main not moved (not checked out)",
          git(work, "rev-parse", "main") == up_head)
    # dirty default branch: fetch, don't merge
    git(work, "switch", "-q", "main")
    Path(work, "README.md").write_text("edited locally\n")
    msg = server._external_sync(work, "main", "work")
    check("dirty main not fast-forwarded",
          git(work, "rev-parse", "HEAD") == up_head and "local changes" in msg, msg)
    git(work, "checkout", "-q", "--", "README.md")
    # no upstream remote at all: degrades to a logged message, no exception
    lone = os.path.join(tmp, "lone")
    git(tmp, "clone", "-q", origin, lone)
    msg = server._external_sync(lone, "main", "lone")
    check("missing upstream remote degrades", "fetch failed" in msg, msg)

    # ── codex override doc ───────────────────────────────────────────────
    print("codex AGENTS.override.md")
    Path(work, "AGENTS.md").write_text("# repo's own agents doc\nbe nice\n")
    git(work, "add", "AGENTS.md")
    git(work, "commit", "-q", "-m", "agents")
    ok = server._ensure_codex_external_doc(work, rule)
    doc = Path(work, "AGENTS.override.md")
    check("written", ok and doc.is_file())
    body = doc.read_text() if doc.is_file() else ""
    check("rule first, repo's AGENTS.md appended",
          body.find("REPORT THE PR LINK") < body.find("be nice") and "be nice" in body)
    check("excluded via .git/info/exclude",
          "AGENTS.override.md" in Path(work, ".git", "info", "exclude").read_text())
    check(".gitignore untouched", not Path(work, ".gitignore").exists())
    check("git sees the tree clean", git(work, "status", "--porcelain") == "")
    ok2 = server._ensure_codex_external_doc(work, rule)
    check("idempotent", ok2 and "AGENTS.override.md" in
          Path(work, ".git", "info", "exclude").read_text()
          and Path(work, ".git", "info", "exclude").read_text().count("AGENTS.override.md") == 1)
    # a tracked override is not ours to rewrite
    tracked = os.path.join(tmp, "tracked")
    make_repo(tracked)
    Path(tracked, "AGENTS.override.md").write_text("theirs\n")
    git(tracked, "add", "-A")
    git(tracked, "commit", "-q", "-m", "override")
    ok3 = server._ensure_codex_external_doc(tracked, rule)
    check("refuses to overwrite a tracked override",
          ok3 is False and Path(tracked, "AGENTS.override.md").read_text() == "theirs\n")

    # ── fork-or-clone decision (gh + clone monkeypatched) ───────────────
    print("provisioning decision")
    calls = []
    real_run = server.subprocess.run

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if cmd[:3] == ["gh", "repo", "view"]:
            perm = fake_run.perm
            if perm is None:
                return types.SimpleNamespace(returncode=1, stdout="",
                                             stderr="GraphQL: Could not resolve to a Repository")
            return types.SimpleNamespace(returncode=0, stderr="", stdout=(
                '{"viewerPermission":"%s","defaultBranchRef":{"name":"master"},'
                '"nameWithOwner":"octocat/Hello-World",'
                '"url":"https://github.com/octocat/Hello-World","isFork":false,"parent":null}'
                % perm))
        if cmd[:3] == ["gh", "repo", "fork"] or cmd[:2] == ["git", "clone"]:
            # stand in for the network: a real clone of the temp upstream,
            # with origin pointing at the fork (fork case) or upstream (direct)
            dest = os.path.join(kw["cwd"], cmd[-1])
            real_run(["git", "clone", "-q", up, dest], check=True)
            src = "https://github.com/clawdbotatg/Hello-World.git" \
                if cmd[0] == "gh" else "https://github.com/octocat/Hello-World"
            real_run(["git", "remote", "set-url", "origin", src], cwd=dest, check=True)
            if cmd[0] == "gh":
                real_run(["git", "remote", "add", "upstream",
                          "https://github.com/octocat/Hello-World.git"], cwd=dest, check=True)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return real_run(cmd, **kw)

    server.subprocess.run = fake_run
    try:
        sm = server.SessionManager.__new__(server.SessionManager)
        sm.projects, sm.sessions = {}, {}
        sm.lock = __import__("threading").RLock()
        sm.save_registry = lambda: None
        sm.broadcast_projects = lambda: None
        sm._unique_project_name = lambda base: base
        real_sync = server._external_sync
        synced = []
        server._external_sync = lambda path, br, name="", budget=None: synced.append((path, br))

        fake_run.perm = "READ"
        p, err = sm.add_external_project("https://github.com/octocat/Hello-World")
        check("READ → project registered cloning", p is not None and err == ""
              and p.kind == "external" and p.status == "cloning")
        # add_external_project fires the thread; run the provisioner inline
        # for determinism by waiting for the thread it started
        deadline = time.time() + 20
        while p.status == "cloning" and time.time() < deadline:
            time.sleep(0.05)
        forked = [c for c in calls if c[:3] == ["gh", "repo", "fork"]]
        check("READ → gh repo fork --clone", bool(forked) and "--clone" in forked[0]
              and forked[0][-1] == p.name, str(forked))
        check("ready with upstream + default branch",
              p.status == "ready" and p.upstream == "https://github.com/octocat/Hello-World"
              and p.default_branch == "master", f"{p.status} {p.error}")
        check("repoUrl = the fork", "clawdbotatg/Hello-World" in p.repo_url, p.repo_url)
        check("upstream remote present",
              git(p.path, "remote", "get-url", "upstream").startswith("https://github.com/octocat/"))
        check("first sync ran", synced and synced[-1][0] == p.path)
        check("re-add returns the same project",
              sm.add_external_project("octocat/Hello-World")[0] is p)
        check("re-add by our FORK's URL returns the same project",
              sm.add_external_project("clawdbotatg/Hello-World")[0] is p)
        # a different owner's repo that merely shares the name must NOT be
        # folded into this folder — it gets its own (name-2)
        calls.clear()
        sm._unique_project_name = lambda base: base + "-2"
        other, err = sm.add_external_project("https://github.com/someone-else/Hello-World")
        check("same name, different repo → separate project",
              other is not None and other is not p and other.name == "Hello-World-2"
              and other.path != p.path, f"{err} {other and other.name}")
        deadline = time.time() + 20
        while other.status == "cloning" and time.time() < deadline:
            time.sleep(0.05)

        # push access → plain clone (fresh name so it doesn't collide)
        calls.clear()
        fake_run.perm = "WRITE"
        sm._unique_project_name = lambda base: base + "-direct"
        p2, err = sm.add_external_project("octocat/Hello-World-direct")
        deadline = time.time() + 20
        while p2.status == "cloning" and time.time() < deadline:
            time.sleep(0.05)
        cloned = [c for c in calls if c[:2] == ["git", "clone"]]
        check("WRITE → git clone (no fork)", bool(cloned)
              and not any(c[:3] == ["gh", "repo", "fork"] for c in calls), str(calls))
        check("direct clone still gets an upstream remote + ready",
              p2.status == "ready"
              and git(p2.path, "remote", "get-url", "upstream") == p2.upstream, p2.error)
        check("direct rule = push access wording", "push access" in p2.standing_rule())

        # gh failure → error card, never ready
        calls.clear()
        fake_run.perm = None
        sm._unique_project_name = lambda base: base + "-nope"
        p3, err = sm.add_external_project("https://github.com/nobody/nothing")
        deadline = time.time() + 20
        while p3.status == "cloning" and time.time() < deadline:
            time.sleep(0.05)
        check("gh view failure → error with reason",
              p3.status == "error" and "Could not resolve" in p3.error, f"{p3.status} {p3.error}")
        check("non-GitHub input rejected up front",
              sm.add_external_project("https://gitlab.com/a/b")[0] is None
              and sm.add_external_project("")[0] is None)

        # a plain gh clone at the same path converts in place
        calls.clear()
        fake_run.perm = "READ"
        gh_proj = server.Project(pid="ghp", name="Hello-World-gh",
                                 path=str(server.PROJECTS_DIR / "Hello-World-gh"),
                                 repo_url="https://github.com/octocat/Hello-World-gh", kind="gh")
        real_run(["git", "clone", "-q", up, gh_proj.path], check=True)
        sm.projects[gh_proj.pid] = gh_proj
        p4, err = sm.add_external_project("https://github.com/octocat/Hello-World-gh")
        deadline = time.time() + 20
        while p4.kind == "external" and not p4.default_branch and time.time() < deadline:
            time.sleep(0.05)
        check("gh clone converted in place", p4 is gh_proj and p4.kind == "external"
              and p4.status == "ready" and p4.default_branch == "master"
              and not any(c[:2] == ["git", "clone"] or c[:3] == ["gh", "repo", "fork"]
                          for c in calls), f"{p4.kind} {p4.status} {calls}")
        server._external_sync = real_sync
    finally:
        server.subprocess.run = real_run

    # ── spawn-time sync gate in create_session ───────────────────────────
    print("spawn-time sync")
    src = open(server.__file__).read()
    i = src.index("def create_session(")
    body = src[i:i + 3000]
    check("create_session syncs external projects before spawning",
          "_external_sync(proj.path" in body and body.index("_external_sync(") < body.index("s.start()"))
    check("…gated on EXTERNAL_SYNC and not for ceremonies",
          "EXTERNAL_SYNC" in body and "not ceremony" in body)


if __name__ == "__main__":
    main()
