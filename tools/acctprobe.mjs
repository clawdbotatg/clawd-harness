// Screenshot the 🧠 accounts panel — the one surface you cannot see from a
// hash route, because it's an overlay. Same headless-shell setup as uiprobe
// (see its header for WHY a local browser and not the claude-in-chrome MCP).
//
//   node acctprobe.mjs                 # against the live harness on :8787
//   HARNESS_PORT=8899 node acctprobe.mjs   # against a scratch copy
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
const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
await page.goto(`http://127.0.0.1:${PORT}/?t=${token}`, { waitUntil: 'networkidle', timeout: 15000 });
await page.waitForTimeout(2500);

// open the accounts overlay the way a human does
const opened = await page.evaluate(() => {
  const b = [...document.querySelectorAll('button,a,span,div')]
    .find(e => (e.textContent || '').trim() === '🧠');
  if (!b) return false;
  b.click(); return true;
});
console.log('clicked 🧠:', opened);
await page.waitForTimeout(2000);

const info = await page.evaluate(() => {
  const cx = document.querySelector('.acct.codexacct');
  return {
    accountCards: document.querySelectorAll('.acct').length,
    codexCard: !!cx,
    codexText: cx ? cx.innerText.replace(/\n+/g, ' | ') : null,
    borderLeft: cx ? getComputedStyle(cx).borderLeftColor : null,
  };
});
console.log(JSON.stringify(info, null, 1));
await page.screenshot({ path: join(HERE, 'acctprobe.png'), fullPage: true });
console.log('screenshot ->', join(HERE, 'acctprobe.png'));
await browser.close();
process.exit(info.codexCard ? 0 : 1);
