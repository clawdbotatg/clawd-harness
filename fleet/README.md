# fleet/ — relay + worker

Drive N machines, each running a clawd-harness, from one phone through one
public relay. This directory is part of the clawd-harness repo; it is deployed
by `git pull`, and the UI is the repo's one `index.html` (the relay serves the
same file and injects `window.__FLEET__`).

- Working on the code: [`CLAUDE.md`](CLAUDE.md).
- Design and trust model: [`../docs/fleet/ARCHITECTURE.md`](../docs/fleet/ARCHITECTURE.md),
  [`../docs/fleet/E2E-PROTOCOL.md`](../docs/fleet/E2E-PROTOCOL.md),
  [`../docs/fleet/SECURITY.md`](../docs/fleet/SECURITY.md).
- Operating it: [`../docs/fleet/DEPLOY.md`](../docs/fleet/DEPLOY.md),
  [`../docs/fleet/RUNBOOK.md`](../docs/fleet/RUNBOOK.md),
  [`../docs/fleet/ADD-MACHINE.md`](../docs/fleet/ADD-MACHINE.md).
- Wire contract: [`../docs/WS-PROTOCOL.md`](../docs/WS-PROTOCOL.md).
