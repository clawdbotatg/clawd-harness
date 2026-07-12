#!/usr/bin/env python3
"""Pick the best Claude subscription for a raw CLI session and launch under it.

The harness routes new sessions to the COOL pool (< SUB_HOT on its most
constrained window) whose WEEKLY window resets soonest (use-it-or-lose-it;
headroom is only the tie-break) — see _route_key in server.py and
docs/fleet/SUB-ROUTING.md. This script applies the SAME rule outside the
harness: it ranks every account dir under ~/.clawd-accounts and execs
`claude` with CLAUDE_CONFIG_DIR set to the winner, so a plain terminal
session lands on the right subscription with zero logging in/out.

  python3 tools/subpick.py             # table + launch claude on the winner
  python3 tools/subpick.py --pick      # print "name<TAB>config_dir", no launch
  python3 tools/subpick.py -- --resume # everything after -- goes to claude

Usage data comes from the harness's poller cache (.clawd-harness.sessions.json,
kept fresh while the harness runs) when it's recent, falling back to a live
probe per account (same mechanism as usage_probe.py). Zero third-party deps.
"""
import datetime, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import usage_probe  # read_credentials / fetch_usage / http_json / constants

REGISTRY = os.path.join(os.path.dirname(HERE), ".clawd-harness.sessions.json")
ACCOUNTS_DIR = os.path.expanduser("~/.clawd-accounts")
SUB_HOT = float(os.environ.get("SUB_HOT", "90"))
CACHE_MAX_AGE = 15 * 60  # trust the harness poller this long


def _parse_reset(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _weekly_reset(windows):
    """Soonest weekly reset — mirrors server._weekly_reset: every weekly
    window's label starts '7d'; the 5h window is deliberately ignored."""
    soonest = None
    for w in windows or []:
        if not str(w.get("label", "")).startswith("7d"):
            continue
        t = _parse_reset(w.get("resets"))
        if t and (soonest is None or t < soonest):
            soonest = t
    return soonest


def _windows_from_live(usage):
    """Normalize the raw usage endpoint payload to the registry's
    [{label, used, resets}] shape (incl. model-scoped weekly limits)."""
    out = []
    for key, label in [("five_hour", "5h"), ("seven_day", "7d")]:
        w = usage.get(key)
        if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
            out.append({"label": label, "used": float(w["utilization"]),
                        "resets": w.get("resets_at")})
    for lim in usage.get("limits") or []:
        if not isinstance(lim, dict) or not isinstance(lim.get("percent"), (int, float)):
            continue
        model = (((lim.get("scope") or {}).get("model") or {}).get("display_name") or "")
        group = {"session": "5h", "weekly": "7d"}.get(lim.get("group"), lim.get("group") or "")
        out.append({"label": f"{group} {model}".strip().lower(),
                    "used": float(lim["percent"]), "resets": lim.get("resets_at")})
    return out


def _probe_live(config_dir):
    creds = usage_probe.read_credentials(config_dir)
    oauth = (creds or {}).get("claudeAiOauth") or {}
    access, refresh = oauth.get("accessToken"), oauth.get("refreshToken")
    if not access:
        return None
    status, usage = usage_probe.fetch_usage(access)
    if status == 401 and refresh:
        st, tok = usage_probe.http_json(
            usage_probe.TOKEN_URL, {"Content-Type": "application/json"},
            {"grant_type": "refresh_token", "refresh_token": refresh,
             "client_id": usage_probe.OAUTH_CLIENT_ID})
        if st == 200 and tok and tok.get("access_token"):
            status, usage = usage_probe.fetch_usage(tok["access_token"])
    if status != 200 or not usage:
        return None
    return _windows_from_live(usage)


def load_accounts():
    """[{name, config_dir, windows}] — harness cache first, live probe stale."""
    now = datetime.datetime.now().timestamp()
    cached = {}
    try:
        for a in json.load(open(REGISTRY)).get("accounts") or []:
            if a.get("ready"):
                cached[a["name"]] = a
    except (OSError, ValueError):
        pass

    roster = {}  # name -> config_dir
    for name, a in cached.items():
        roster[name] = a.get("config_dir") or os.path.join(ACCOUNTS_DIR, name)
    if not roster and os.path.isdir(ACCOUNTS_DIR):
        for name in sorted(os.listdir(ACCOUNTS_DIR)):
            d = os.path.join(ACCOUNTS_DIR, name)
            if os.path.isdir(d):
                roster[name] = d

    out = []
    for name, config_dir in roster.items():
        u = (cached.get(name) or {}).get("usage") or {}
        windows = u.get("windows")
        if not windows or now - (u.get("checkedAt") or 0) > CACHE_MAX_AGE:
            live = _probe_live(config_dir)
            if live is not None:
                windows = live
        if windows:
            out.append({"name": name, "config_dir": config_dir, "windows": windows})
    return out


def route_key(a):
    """Mirror of server._route_key: (hot?, no-reset?, weekly reset, pct)."""
    pct = max((w.get("used") or 0.0) for w in a["windows"])
    reset = _weekly_reset(a["windows"])
    return (pct >= SUB_HOT, reset is None, reset or 0.0, pct)


def main():
    argv = sys.argv[1:]
    pick_only = "--pick" in argv
    claude_args = argv[argv.index("--") + 1:] if "--" in argv else []

    accounts = load_accounts()
    if not accounts:
        print("no accounts with usable usage data — is the harness registry "
              f"present at {REGISTRY}?", file=sys.stderr)
        return 1
    accounts.sort(key=route_key)

    for i, a in enumerate(accounts):
        pct = max(w.get("used") or 0.0 for w in a["windows"])
        reset = _weekly_reset(a["windows"])
        when = (datetime.datetime.fromtimestamp(reset).strftime("%a %H:%M")
                if reset else "?")
        hot = "HOT" if pct >= SUB_HOT else "   "
        mark = "->" if i == 0 else "  "
        print(f"{mark} {a['name']:10s} {hot}  worst {pct:5.1f}%  weekly resets {when}")

    best = accounts[0]
    if pick_only:
        print(f"{best['name']}\t{best['config_dir']}")
        return 0

    claude = usage_probe.subprocess.run(["/usr/bin/which", "claude"], capture_output=True,
                                        text=True).stdout.strip() or \
        os.path.expanduser("~/.local/bin/claude")
    env = dict(os.environ, CLAUDE_CONFIG_DIR=best["config_dir"])
    print(f"\nlaunching claude on '{best['name']}' "
          f"(CLAUDE_CONFIG_DIR={best['config_dir']})\n")
    sys.stdout.flush()  # execve replaces the process; unflushed output is lost
    os.chdir(os.path.expanduser("~"))
    os.execve(claude, [claude] + claude_args, env)


if __name__ == "__main__":
    sys.exit(main())
