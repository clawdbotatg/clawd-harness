"""The fleet projectKey, in Python — ONE copy for the worker and the relay.

`index.html projectKey()` is what the UI routes on and what an iron stores as
its member `keys`: a private folder is machine-qualified (`local:<machine>:
<path>`), a repo is its normalized remote (so the same GitHub repo groups once
across machines), anything else is `name:<name>`. The worker mirrors it for
push deep links (`fleet/worker.py _project_key`); the relay mirrors it to
resolve which iron a session's project belongs to when that session writes to
an iron's to-do list (`/todo/agent`). Both go through here. MUST stay
byte-identical to the JS (normRepo + projectKey) or deep links and iron
membership silently stop matching.
"""
import re


def norm_repo(url):
    """Mirror of index.html normRepo(): canonicalize a git remote."""
    s = (url or "").strip()
    if not s:
        return ""
    s = re.sub(r"^git@([^:]+):", r"\1/", s)            # git@host:owner/repo → host/owner/repo
    s = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", s)  # strip scheme
    s = re.sub(r"\.git$", "", s, flags=re.I)            # drop trailing .git
    s = re.sub(r"/+$", "", s)                           # drop trailing slash
    return s.lower()


def project_key(machine, meta):
    """meta = {name, repoUrl, kind, path} (a harness projectMeta row)."""
    meta = meta or {}
    if meta.get("kind") == "local":
        return f"local:{machine}:{meta.get('path') or meta.get('name') or ''}"
    return norm_repo(meta.get("repoUrl")) or ("name:" + (meta.get("name") or ""))


def candidate_keys(machine, meta):
    """Every key form this project could have been stored under in an iron:
    the canonical key first, then the name form (a machine that couldn't
    report the repo URL when the iron was built stored `name:<x>`; the UI
    folds the two — ironKeyCanon — and so must we)."""
    meta = meta or {}
    out = [project_key(machine, meta)]
    name = (meta.get("name") or "").strip()
    if name and meta.get("kind") != "local":
        alt = "name:" + name
        if alt not in out:
            out.append(alt)
    return out


def iron_for_project(irons, machine, meta):
    """The iron (dict with `keys`) holding this project, or None. Exact key
    match first; then the basename fold the UI uses when one side keyed by
    URL and the other by name (…/<name> ↔ name:<name>)."""
    cands = candidate_keys(machine, meta)
    for iron in irons or []:
        keys = iron.get("keys") or []
        if any(k in keys for k in cands):
            return iron
    name = ((meta or {}).get("name") or "").strip().lower()
    if not name or (meta or {}).get("kind") == "local":
        return None
    for iron in irons or []:
        for k in iron.get("keys") or []:
            base = k[5:] if k.startswith("name:") else k.rsplit("/", 1)[-1]
            if base.lower() == name and not k.startswith("local:"):
                return iron
    return None
