#!/usr/bin/env bash
# Keep the relay box's checkout current, so "deploy = push to main" is TRUE
# EVERYWHERE — not just on the harness machines.
#
# Why this exists: every harness self-updates (server.py auto_update_loop), but
# the relay box runs no harness, so nothing pulled it. It is also the box that
# SERVES THE FLEET UI (index.html, read fresh per request). The result was a
# silent, invisible failure mode: you push a UI change, every machine takes it,
# and h.atg.link keeps serving a stale page for days because a human forgot one
# ssh. It bit us on 2026-08-07 — the fleet UI sat 8 commits behind while the
# harnesses were all current.
#
# Deliberately conservative, mirroring the harness's own auto_update rules:
#   - only on main, only with a clean worktree (a live edit is never clobbered)
#   - fetch + merge --ff-only, never a rebase or a merge commit
#   - a UI-only change needs NO restart (the relay reads index.html per request)
#   - fleet/ code changed  -> restart the relay + worker (they are Restart=always
#     and every client reconnects; this is routine)
#   - controller/ changed  -> WARN ONLY. Restarting mid-turn would kill a running
#     PM turn, so that stays a human's call.
# Disable with: sudo systemctl disable --now clawd-fleet-pull.timer
set -uo pipefail
REPO="${CLAWD_REPO:-/home/ubuntu/clawd-harness}"
cd "$REPO" || { echo "no repo at $REPO"; exit 0; }

branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
[ "$branch" = "main" ] || { echo "on '$branch', not main — skipping"; exit 0; }
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "worktree dirty — skipping (someone is editing live)"
    exit 0
fi

before=$(git rev-parse HEAD)
git fetch --quiet origin main || { echo "fetch failed"; exit 0; }
git merge --ff-only --quiet origin/main 2>/dev/null || {
    echo "not fast-forwardable — skipping (diverged; needs a human)"
    exit 0
}
after=$(git rev-parse HEAD)
[ "$before" = "$after" ] && exit 0

changed=$(git diff --name-only "$before" "$after")
echo "pulled $(git rev-parse --short "$before") -> $(git rev-parse --short "$after")"
echo "$changed" | sed 's/^/    /'

if echo "$changed" | grep -q '^fleet/.*\.py$'; then
    echo "fleet code changed — restarting relay + worker"
    sudo systemctl restart clawd-fleet-relay clawd-fleet-worker
fi
if echo "$changed" | grep -q '^controller/'; then
    echo "WARNING: controller code changed but NOT restarted (a restart kills a"
    echo "         running PM turn). Run when quiet:"
    echo "         sudo systemctl restart clawd-controller"
fi
exit 0
