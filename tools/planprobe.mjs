// Drive renderCodexPlanCard() — the FLEET roll-up card — in a real DOM.
//
// The "⚡ plans" section only renders in fleet mode, which lives behind the
// relay's passkey, so it can't be reached from a local harness. Instead we load
// the real page, stub accountsInfoFor() with two machines' payloads, and call
// the real function. Catches what a syntax check can't: a bad field name, a
// helper that isn't in scope, a card that renders blank.
import { chromium } from 'playwright-core';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = dirname(HERE);
const PORT = process.env.HARNESS_PORT || '8787';

function findChromium() {
  const cache = join(process.env.HOME, 'Library/Caches/ms-playwright');
  if (!existsSync(cache)) return null;
  const shells = readdirSync(cache).filter(d => d.startsWith('chromium_headless_shell-')).sort().reverse();
  for (const d of shells)
    for (const arch of ['mac-arm64', 'mac-x64']) {
      const bin = join(cache, d, `chrome-headless-shell-${arch}`, 'chrome-headless-shell');
      if (existsSync(bin)) return bin;
    }
  return null;
}
const exec = findChromium();
if (!exec) { console.error('no cached playwright chromium'); process.exit(2); }
let token = '';
try { token = readFileSync(join(ROOT, '.clawd-harness.token'), 'utf8').trim(); } catch {}

const browser = await chromium.launch({ executablePath: exec });
const page = await browser.newPage({ viewport: { width: 1200, height: 700 } });
await page.goto(`http://127.0.0.1:${PORT}/?t=${token}`, { waitUntil: 'networkidle', timeout: 15000 });
await page.waitForTimeout(1500);

const out = await page.evaluate(() => {
  const now = Date.now() / 1000;
  const fake = {
    heart: { codex: { engine: 'codex', status: 'ready', email: 'a@example.com',
                      plan: 'plus', pct: 4, checkedAt: now - 30,
                      windows: [{ pct: 4, windowMins: 10080, resetsAt: now + 600000 }],
                      credits: { has: false, unlimited: false, balance: '0' }, error: '' } },
    head:  { codex: { engine: 'codex', status: 'ready', email: 'a@example.com',
                      plan: 'plus', pct: 11, checkedAt: now - 400,
                      windows: [{ pct: 11, windowMins: 10080, resetsAt: now + 600000 }],
                      credits: { has: false, unlimited: false, balance: '0' }, error: '' } },
    sat:   { codex: null },                        // codex not signed in here
  };
  window.accountsInfoFor = (mid) => fake[mid] || null;
  const grid = document.createElement('div');
  document.body.appendChild(grid);
  renderCodexPlanCard(grid, [{ id: 'heart', name: 'clawd-heart' },
                             { id: 'head', name: 'clawd-head' },
                             { id: 'sat', name: 'clawd-sat' }]);
  const card = grid.querySelector('.acct.codexacct');
  return {
    cards: grid.querySelectorAll('.acct').length,
    text: card ? card.innerText.replace(/\n+/g, ' | ') : null,
    chips: [...grid.querySelectorAll('.amchip')].map(c => c.textContent),
    bars: grid.querySelectorAll('.awin').length,
    headline: card ? (card.querySelector('.abig') || {}).textContent : null,
  };
});
console.log(JSON.stringify(out, null, 1));
await browser.close();
// one pooled card (both machines share an email), a window bar, and a
// per-machine chip each plus the "not routed" tag
process.exit(out.cards === 1 && out.bars === 1 && out.chips.length === 3 ? 0 : 1);
