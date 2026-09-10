# Security checks and deployment

`python3 test_origin_gate.py` checks actual harness/controller HTTP handlers
against foreign Origin and rebinding Host headers. It also checks frame limits.
`python3 fleet/test_relay_gate.py` checks the public gate using isolated state:
unknown roles, unauthenticated roster/upload access, upload quotas, message
limits, login-slot limits, login expiry, valid session survival and message spam.
`node tools/fleetprobe.mjs` checks that reconnects do not repeat the automatic
passkey prompt, and the unlock button still works. These run in checkall.

`python3 tools/shipcheck.py --wait 2100` must pass after deployment. All machines
in the relay's worker allowlist must report matching running worker AND harness
builds, including machines switched off in the UI. A worker-only match is not
proof that the harness restarted. The relay/controller services themselves
must be restarted after their code changes; verify their running endpoints.

## nginx

Install `fleet/deploy/harness-limits.conf` under `/etc/nginx/conf.d/` and
`fleet/deploy/harness-ws-limits.conf` under `/etc/nginx/snippets/`. In h.atg.link
only, duplicate its existing relay proxy location as `location = /ws` and
include the latter snippet there. Preserve proxy headers, access logging without
query strings and timeouts. Do not apply these limits to unrelated vhosts.

Limits: 32 open WebSocket connections per source IP, 5 handshake requests/sec
with a burst of 30. Established frames do not consume the handshake quota.
The shared household and nine workers have room within those limits.
See nginx's [connection limits](https://nginx.org/en/docs/http/ngx_http_limit_conn_module.html)
and [request limits](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html).

Back up each config before editing. Run `sudo nginx -t` before
`sudo systemctl reload nginx`. Restore the backups and reload to roll back.
The September 10 installation saved backups on the relay under
`/tmp/<config basename>.security-20260910.bak`; local before/after copies are
`/tmp/harness-nginx-{before,after}-20260910.conf`.

## Scope

These checks address known entry paths and resource abuse. They do not prove
absence of prior compromise or protection from every distributed denial of
service attack. The relay host remains trusted for the served UI, PM controller
and uploads; uploads are passkey-session authenticated over HTTPS, but are not
inside the end-to-end encrypted worker channel. Local processes retain access
to the loopback services. No passkey enrollment or cadence changes are involved.
