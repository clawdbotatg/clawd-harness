#!/usr/bin/env python3
"""Phase 0 probe for subscription routing (docs/fleet/SUB-ROUTING.md).

Reads a Claude Code OAuth credential (macOS Keychain or .credentials.json),
hits the undocumented usage endpoint, and prints per-window headroom.
Validates the whole mechanism with zero third-party installs.

  python3 tools/usage_probe.py [CONFIG_DIR]

No arg = the default ~/.claude login. Exit 0 iff usage was fetched and parsed.

READ-ONLY on the login: it never refreshes the token. A refresh ROTATES the
grant — the old refresh token dies and the new one must be written back to the
store, which only the real CLI does. This probe used to refresh on a 401 and
throw the new token away, which can strand the very login you were diagnosing
(EXPECTATIONS.md, single-consumer rule). An expired access token is reported
as such; a live claude session on that login renews it.
Mechanism credit: github.com/dennisonbertram/claw-router (studied, not run).
"""
import hashlib, json, os, subprocess, sys, unicodedata, urllib.request

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
WINDOWS = [("five_hour", "5h session"), ("seven_day", "7d total"),
           ("seven_day_opus", "7d Opus"), ("seven_day_sonnet", "7d Sonnet")]


def keychain_service(config_dir):
    """Mirror Claude Code's derivation: no dir -> base name; else -sha256(NFC(dir))[0:8]."""
    if not config_dir:
        return "Claude Code-credentials"
    h = hashlib.sha256(unicodedata.normalize("NFC", config_dir).encode()).hexdigest()[:8]
    return f"Claude Code-credentials-{h}"


def read_credentials(config_dir):
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", keychain_service(config_dir),
             "-a", os.environ.get("USER", ""), "-w"],
            capture_output=True, text=True, timeout=15)
        if out.returncode == 0 and out.stdout.strip():
            return json.loads(out.stdout.strip())
    except Exception:
        pass
    path = os.path.join(config_dir or os.path.expanduser("~/.claude"), ".credentials.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def http_json(url, headers, body=None):
    req = urllib.request.Request(url, headers=headers,
                                 data=json.dumps(body).encode() if body else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        print(f"  network error: {e}", file=sys.stderr)
        return None, None


def fetch_usage(access):
    return http_json(USAGE_URL, {"Authorization": f"Bearer {access}",
                                 "anthropic-beta": OAUTH_BETA,
                                 "Content-Type": "application/json"})


def main():
    config_dir = sys.argv[1] if len(sys.argv) > 1 else ""
    label = config_dir or "~/.claude (default)"
    creds = read_credentials(config_dir)
    if not creds:
        print(f"FAIL: no credentials found for {label}"); return 1
    oauth = creds.get("claudeAiOauth") or {}
    access = oauth.get("accessToken")
    if not access:
        print(f"FAIL: credential blob has no accessToken ({label})"); return 1
    print(f"credential: OK ({label}, keychain service '{keychain_service(config_dir)}')")

    status, usage = fetch_usage(access)
    if status == 401:
        print("FAIL: access token expired (401). Not refreshing — that would rotate "
              "the login's grant and discard the new token. Start (or prompt) a "
              "claude session on this login to renew it, then re-run.")
        return 1
    if status != 200 or usage is None:
        print(f"FAIL: usage endpoint returned {status}"); return 1

    print("usage endpoint: OK")
    worst = 0.0
    for key, lab in WINDOWS:
        w = usage.get(key)
        if w is None:
            continue
        util = w.get("utilization") if isinstance(w, dict) else w
        resets = w.get("resets_at", "") if isinstance(w, dict) else ""
        if isinstance(util, (int, float)):
            worst = max(worst, util)
            print(f"  {lab:11s} {util:5.1f}% used" + (f"   resets {resets}" if resets else ""))
    # model-scoped caps (e.g. the Fable weekly limit) live only in `limits`
    group_lab = {"session": "5h", "weekly": "7d"}
    for lim in usage.get("limits") or []:
        if not isinstance(lim, dict):
            continue
        pct = lim.get("percent")
        model = (((lim.get("scope") or {}).get("model") or {}).get("display_name") or "")
        if not isinstance(pct, (int, float)) or not model:
            continue
        worst = max(worst, pct)
        lab = f"{group_lab.get(lim.get('group'), lim.get('group') or '')} {model}".strip()
        resets = lim.get("resets_at") or ""
        print(f"  {lab:11s} {pct:5.1f}% used" + (f"   resets {resets}" if resets else ""))
    print(f"headroom (100 - most constrained window): {100 - worst:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
