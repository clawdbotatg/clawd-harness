#!/usr/bin/env python3
"""Token accounts: a `claude setup-token` in .clawd-harness.env runs a plan
with no sign-in (docs/fleet/SUB-ROUTING.md § Token accounts).

Pins what makes them safe: the credential gates see a healthy login that
never needs rotating, a child gets its own account's token and never an
inherited one, usage comes from the probe's rate-limit headers, the Fable
probe feeds the capability gate, and a same-pool tie routes to the token.
Pure helpers only — no SessionManager, no network, no spawn.

    python3 test_token_accounts.py
"""
import sys
import time
import types

import server
sys.path.insert(0, str(server.HERE / "tools"))
import install_claude_tokens  # noqa: E402

TOK = "sk-ant-oat01-test"
DIR = "/tmp/clawd-test-accounts/tok"
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def with_token(fn):
    def run():
        server._TOKEN_DIRS[server._norm_config_dir(DIR)] = TOK
        try:
            fn()
        finally:
            server._TOKEN_DIRS.clear()
    run.__name__ = fn.__name__
    return run


HDRS = {"Anthropic-Ratelimit-Unified-5h-Utilization": "0.06",
        "anthropic-ratelimit-unified-5h-reset": "1791324600",
        "anthropic-ratelimit-unified-7d-utilization": "0.19",
        "anthropic-ratelimit-unified-7d-reset": "1791626400",
        "anthropic-organization-id": "org-1"}


def probes(*answers):
    """Stub _probe_message with canned (status, headers); records models."""
    calls, it = [], iter(answers)
    def stub(tok, model):
        calls.append(model)
        return next(it)
    server._probe_message = stub
    return calls


@case
@with_token
def test_store_gates_see_a_healthy_unrotatable_login():
    assert server._has_creds(DIR)
    assert server._creds_state(DIR) == "present"
    assert server._login_verdict(DIR) == (None, None)
    assert server._cred_sig(DIR)
    assert server.Account("tok", DIR).token
    assert not server.Account("other", "/tmp/clawd-test-accounts/other").token


@case
@with_token
def test_child_env_carries_only_its_own_token():
    env = server._auth_env({"CLAUDE_CODE_OAUTH_TOKEN": "inherited"}, DIR)
    assert env == {"CLAUDE_CONFIG_DIR": DIR, "CLAUDE_CODE_OAUTH_TOKEN": TOK}
    env = server._auth_env({"CLAUDE_CODE_OAUTH_TOKEN": "inherited",
                            "CLAUDE_CONFIG_DIR": "/x"}, "")
    assert env == {}, env                      # default login: nothing pinned
    env = server._auth_env({}, "/tmp/clawd-test-accounts/keychain")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


@case
def test_headers_parse_into_usage_windows():
    pct, windows, org = server._usage_from_headers(HDRS)
    assert pct == 19.0 and org == "org-1"
    assert [w["label"] for w in windows] == ["5h", "7d"]
    assert server._parse_reset(windows[1]["resets"]) == 1791626400
    assert server._usage_from_headers({"x": "1"}) is None
    assert server._usage_from_headers(None) is None


@case
def test_fable_probe_then_cadence():
    cache = {}
    calls = probes((200, HDRS))
    pct, _, ident = server._fetch_usage_token(TOK, cache, want_ident=True)
    assert calls == [server.FABLE_PROBE_MODEL] and ident["fable"] is True
    assert pct == 19.0 and ident["org"] == "org-1"
    calls = probes((200, HDRS))                # inside TOKEN_FABLE_EVERY
    _, _, ident = server._fetch_usage_token(TOK, cache, want_ident=True)
    assert calls == [server.PROBE_MODEL] and ident["fable"] is None


@case
def test_fable_refused_falls_back_to_haiku_for_numbers():
    cache = {}
    calls = probes((400, {}), (200, HDRS))
    pct, _, ident = server._fetch_usage_token(TOK, cache, want_ident=True)
    assert calls == [server.FABLE_PROBE_MODEL, server.PROBE_MODEL]
    assert ident["fable"] is None and pct == 19.0
    assert cache["fable_at"]                   # refusal counts as checked


@case
def test_revoked_rate_limited_and_offline():
    probes((401, {}))
    assert server._fetch_usage_token(TOK, {}) == server.AUTH_FAIL
    cache = {"fable_at": time.time()}
    probes((429, {}))
    assert server._fetch_usage_token(TOK, cache) == server.RATE_LIMITED
    assert cache["no_poll_until"] > time.time()
    cache = {}
    probes((None, None), (None, None))
    assert server._fetch_usage_token(TOK, cache) is None
    assert "fable_at" not in cache             # never got an answer: retry
    probes((429, HDRS))                        # a walled plan still reports
    assert server._fetch_usage_token(TOK, {"fable_at": time.time()})[0] == 19.0


@case
@with_token
def test_same_pool_tie_routes_to_the_token():
    def acct(name, cdir):
        a = server.Account(name, config_dir=cdir, ready=True)
        a.usage = {"pct": 20.0, "checkedAt": time.time(),
                   "windows": [{"key": "seven_day", "label": "7d", "used": 20.0,
                                "resets": "2030-01-01T00:00:00+00:00"}]}
        a.fable_seen = time.time()
        return a
    m = types.SimpleNamespace()
    key = server.SessionManager._route_key.__get__(m)
    old = acct("aaa", "/tmp/clawd-test-accounts/keychain")
    tok = acct("zzz", DIR)
    assert min([old, tok], key=key) is tok


@case
def test_installer_merges_in_place():
    env = "BANKR_API=bankr\nCLAUDE_TOKEN_A=sk-ant-oat01-old\n"
    new, changed = install_claude_tokens.merge(
        env, {"CLAUDE_TOKEN_A": "sk-ant-oat01-new",
              "CLAUDE_TOKEN_B": "sk-ant-oat01-b"})
    assert new == ("BANKR_API=bankr\nCLAUDE_TOKEN_A=sk-ant-oat01-new\n"
                   "CLAUDE_TOKEN_B=sk-ant-oat01-b\n"), new
    assert changed == ["CLAUDE_TOKEN_A", "CLAUDE_TOKEN_B"]
    assert install_claude_tokens.merge(new, {"CLAUDE_TOKEN_B":
                                             "sk-ant-oat01-b"})[1] == []


if __name__ == "__main__":
    real = server._probe_message
    failed = 0
    for fn in CASES:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        finally:
            server._probe_message = real
    sys.exit(1 if failed else 0)
