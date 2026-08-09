// Guards the 🧠 panel's **capability marker** (2026-08-09): a subscription whose
// plan can't do fable is skipped by the router, and the card has to SAY that.
//
// Why a probe and not an eyeball: the card only differs from a healthy one when
// the server sends `routable:false`, and the machine you're testing on may not
// have such an account today — so the bug ships invisibly. This drives the real
// `renderAccountsPanel()` with a stubbed frame instead, which means it needs no
// live account in that state, subscribes to nothing, and touches no session.
//
// The three things it asserts are the ones that make the card honest:
//   1. an unroutable pool is visibly out of rotation (dashed + dimmed),
//   2. it says WHY, in words, on the card,
//   3. it is NOT dressed up as signed out — the login works, and a card that
//      cried "signed out" would send the user to a re-sign-in they don't need.
//
//   node capprobe.mjs                      # against the live harness on :8787
//   HARNESS_PORT=8899 node capprobe.mjs    # against a scratch copy
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
await page.waitForTimeout(2000);

const out = await page.evaluate(() => {
  const win = (label, used) => ({ key: label.replace(/ /g, '_'), label, used,
                                  resets: new Date(Date.now() + 6e8).toISOString() });
  // two pools, same shape, one difference: `routable`. Anything the probe sees
  // between them is caused by the capability gate and nothing else.
  const frame = {
    type: 'accounts', active: 'good', auto: true, best: 'good',
    accounts: [
      { name: 'nofable', email: 'slop@example.com', orgUuid: 'org-a',
        orgName: 'Slop Org', tier: 'default_claude_ai', status: 'ready',
        active: false, fable: false, routable: false,
        usagePct: 3, headroom: 97, windows: [win('7d', 3)],
        checkedAt: Date.now() / 1000, error: '', configDir: '/tmp/nofable' },
      { name: 'good', email: 'ok@example.com', orgUuid: 'org-b',
        orgName: 'Good Org', tier: 'default_claude_max_20x', status: 'ready',
        active: true, fable: true, routable: true,
        usagePct: 60, headroom: 40, windows: [win('7d', 60), win('7d fable', 60)],
        checkedAt: Date.now() / 1000, error: '', configDir: '/tmp/good' },
    ],
  };
  const host = document.createElement('div');
  host.style.cssText = 'position:fixed;left:0;top:0;width:520px;z-index:99999;background:var(--bg)';
  document.body.appendChild(host);
  renderAccountsPanel(host, frame, 'self');

  const cards = [...host.querySelectorAll('.acct')];
  const find = t => cards.find(c => (c.innerText || '').includes(t)) || null;
  const bad = find('Slop Org'), ok = find('Good Org');
  const read = c => c && {
    nocap: c.classList.contains('nocap'),
    border: getComputedStyle(c).borderTopStyle,
    opacity: parseFloat(getComputedStyle(c).opacity),
    text: (c.innerText || '').replace(/\n+/g, ' | '),
  };
  return { cards: cards.length, bad: read(bad), ok: read(ok) };
});

console.log(JSON.stringify(out, null, 1));
await page.screenshot({ path: join(HERE, 'capprobe.png'), fullPage: false });
console.log('screenshot ->', join(HERE, 'capprobe.png'));
await browser.close();

const fail = [];
if (!out.bad || !out.ok) fail.push('both stub cards should render');
else {
  // 1. visibly out of rotation
  if (!out.bad.nocap) fail.push('unroutable card is missing the .nocap class');
  if (out.bad.border !== 'dashed') fail.push(`unroutable border is ${out.bad.border}, want dashed`);
  if (!(out.bad.opacity < 1)) fail.push('unroutable card is not dimmed');
  // 2. says why, in words
  if (!/doesn.t carry fable/i.test(out.bad.text)) fail.push('unroutable card never says why');
  // 3. not dressed up as broken — and the healthy card is untouched
  if (/signed out|needs re-sign-in/i.test(out.bad.text))
    fail.push('unroutable card reads as signed out — it is not');
  if (out.ok.nocap || out.ok.opacity < 1 || /carry fable/i.test(out.ok.text))
    fail.push('the routable card picked up the marker too');
}
if (fail.length) { fail.forEach(f => console.error('FAIL:', f)); process.exit(1); }
console.log('ok — unroutable pool renders as out-of-rotation, with the reason');
