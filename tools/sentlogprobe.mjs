// sentlogprobe — the 🕘 sent history (2026-08-26). A long send DELIVERS in
// full (bracketed paste, 8f1aeaf) but claude's TUI echoes only its tail — and
// a mid-turn send rides in as steering with no echo at all — so the harness
// keeps every composer send in a localStorage ring, recoverable via the 🕘
// button. This guards the chain the feature is: deliverSend records the full
// text (quick chips excluded), the modal shows it whole and newest-first, the
// ring is capped, and ↩ puts the exact text back in the composer.
//
// Safe: lands on the sessions rung (#/p/self) — subscribes to nothing, claims
// no PTY size — and calls recordSent()/deliverSend() with the WebSocket send
// stubbed out, so nothing reaches a real session.
import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

function findChromium() {
  const cache = join(process.env.HOME, 'Library/Caches/ms-playwright');
  const shells = readdirSync(cache).filter(d => d.startsWith('chromium_headless_shell-')).sort().reverse();
  for (const d of shells)
    for (const arch of ['mac-arm64', 'mac-x64']) {
      const bin = join(cache, d, `chrome-headless-shell-${arch}`, 'chrome-headless-shell');
      if (existsSync(bin)) return bin;
    }
  return null;
}

const token = readFileSync(join(ROOT, '.clawd-harness.token'), 'utf8').trim();
const url = `http://127.0.0.1:8787/?t=${token}#/p/self`;
const browser = await chromium.launch({ executablePath: findChromium() });
const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
await page.goto(url, { waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => typeof recordSent === 'function').catch(() => {});
await page.waitForTimeout(600);

const r = await page.evaluate(() => {
  const out = {};
  localStorage.removeItem('cc_sent');
  // deliverSend must record — with the wire stubbed so nothing is really sent
  const realHsend = hsend; hsend = () => true;
  lastRx = Date.now();                                  // keep deliverSend's liveness probe quiet
  const CID = '00000000-probe-sentlog';
  const BIG = 'PROBE-HEAD ' + 'x'.repeat(1300) + ' PROBE-TAIL';   // dictation-shaped: one huge line
  deliverSend(CID, BIG, BIG);
  deliverSend(CID, 'canned chip text', 'canned chip text', 'quick');
  hsend = realHsend;
  clearOutbox(CID);
  const ring = sentRing();
  out.recorded  = ring.length === 1;                    // typed send in, quick chip out
  out.intact    = ring[0] && ring[0].text === BIG;      // head AND tail survive
  // the modal shows the whole text, and ↩ restores it to the composer
  showSentLog();
  out.modalUp   = !document.getElementById('sentlog').hidden;
  const item = document.querySelector('#sentloglist .sl-text');
  out.shown     = !!item && item.textContent.startsWith('PROBE-HEAD') && item.textContent.endsWith('PROBE-TAIL');
  document.querySelector('#sentloglist .sl-meta button + button').click();   // ↩ composer
  out.restored  = box.value === BIG;
  out.modalDown = document.getElementById('sentlog').hidden;
  box.value = ''; autosizeBox(); clearDraft && clearDraft();
  // the ring is a RING: entry 31 evicts the oldest, newest stays first
  for (let i = 0; i < 31; i++) recordSent(CID, 'filler ' + i);
  const full = sentRing();
  out.capped = full.length === 30 && full[0].text === 'filler 30';
  localStorage.removeItem('cc_sent');
  return out;
});
console.log('SENTLOG:', JSON.stringify(r));

const ok = r.recorded && r.intact && r.modalUp && r.shown && r.restored && r.modalDown && r.capped;
console.log(ok ? 'PASS — sends archived whole, quick chips skipped, modal shows/restores, ring capped'
              : 'FAIL');
await browser.close();
process.exit(ok ? 0 : 1);
