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

## Public exposure review — 2026-09-15

Reviewed the public endpoint, relay/worker authentication and live process
configuration. Anonymous HTTP probes rejected private controller, documents,
skills, dictation words, uploads, tool lookup, voice and SMS requests. Neither
`.git/config` nor `fleet/fleet.env` was served. An anonymous mobile WebSocket
received `authRequired`, not a machine roster. The running relay requires
passkeys, binds loopback, has explicit worker/controller secrets and a machine
allowlist. No public test passkey is enrolled. All configured fleet machines
passed shipcheck's running-code checks.

The nginx vhost had diverged: `sites-enabled/h.atg.link` was a separate regular
file with WebSocket limits, while `sites-available/h.atg.link` contained the
newer docs limits but lacked those WebSocket limits. The docs limits were
therefore NOT active. Merged the protections, made sites-enabled a symlink to
sites-available, validated with `nginx -t`, and gracefully reloaded nginx.
Backups of both originals are in
`/etc/nginx/harness-security-backup-20260916-014821/` (UTC timestamp).

Additional live vhost protections:

- Exact `/ws` rejects a nonempty Origin other than `https://h.atg.link`.
- `/pm` rejects foreign/null Origin and `Sec-Fetch-Site: cross-site`, including
  requests from sibling subdomains that browsers consider same-site. Verified
  using an existing session: same-origin read returned 200; sibling-origin read
  returned 403. Nonbrowser requests still require existing app authentication.
- `X-Frame-Options: SAMEORIGIN` and CSP `frame-ancestors 'self'; object-src
  'none'; base-uri 'self'` prevent foreign embedding while keeping PM iframes.
- `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and HSTS
  `max-age=31536000` are sent on HTTPS responses, including errors.
- The vhost defaults to the existing `noquery` access-log format, including
  voice/tool routes; query credentials are not recorded in new access entries.

Verified the headers and rejection behavior over public HTTPS after reload.
Local tests passed: relay gate (18 checks), WebAuthn verifier, E2E handshake,
MITM rejection, Python/browser crypto interoperability, doc-store security
(13 tests), and harness/controller origin gates. E2E tests require the Homebrew
Python with `cryptography`; the system Python lacked that dependency.

Restricted local token/env/generated hook files to mode 0600. Known local
secret values did not occur in currently tracked files. This was not a complete
git-history secret scan, host-compromise investigation, or audit of the other
applications sharing the relay host. One provisioned hardware-key credential
allows touch without PIN/biometric verification; its possession is sufficient
for that credential. The seven-day session cadence was unchanged.

Future nginx changes must inspect `nginx -T`, not just sites-available. Preserve
both docs and WebSocket limits and the browser protections above. The original
`setup_tls.sh` is a bootstrap script, not a safe update of this live vhost.
