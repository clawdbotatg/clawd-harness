// closedprobe — the 🗃️ closed-sessions modal (2026-09-05). The tab ✕ has no
// confirm and three sessions were lost to it that day; the harness now files a
// row per close (`closed` frames) and ↩ asks the owning machine to `reopen` it.
// Guards the client half: frames render newest-first, the filter narrows, a
// second frame RECONCILES (row nodes survive — repaint, don't rebuild), and a
// real click on ↩ sends {type:'reopen', cid} then closes the modal.
//
// Safe: lands on the sessions rung (#/p/self), feeds fake `closed` frames
// straight into onClosedFrame() with the wire stubbed, so nothing reaches a
// real session and no session is actually reopened.
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
await page.waitForFunction(() => typeof onClosedFrame === 'function').catch(() => {});
await page.waitForTimeout(600);

const r1 = await page.evaluate(() => {
  const out = {};
  window.__sent = [];
  window.__realHsend = hsend; hsend = (f) => { window.__sent.push(f); return true; };
  const now = Date.now() / 1000;
  const A = { cid: 'aaaa-probe', engine: 'claude', pid: 'self', project: 'clawd-harness', session_id: 's-a',
              title: 'Alpha closed', desc: 'first row', prompt_count: 4, closed_at: now - 60, reason: 'closed', resumable: true };
  const B = { cid: 'bbbb-probe', engine: 'codex', pid: 'self', project: 'clawd-harness', session_id: 's-b',
              title: 'Beta closed', desc: 'second row', prompt_count: 1, closed_at: now - 3600, reason: 'closed', resumable: true };
  onClosedFrame(null, [B, A]);                         // server order irrelevant: the modal sorts
  showClosedLog();
  out.modalUp = !document.getElementById('closedlog').hidden;
  const items = [...document.querySelectorAll('#closedloglist .cl-item')];
  out.two = items.length === 2;
  out.newestFirst = items[0].querySelector('.cl-title').textContent === 'Alpha closed';
  out.codexTag = items[1].querySelector('.cl-when').textContent.includes('codex');
  const f = document.getElementById('closedfilter');
  f.value = 'beta'; f.dispatchEvent(new Event('input'));
  const vis = [...document.querySelectorAll('#closedloglist .cl-item')].filter(n => !n.hidden);
  out.filtered = vis.length === 1 && vis[0].querySelector('.cl-title').textContent === 'Beta closed';
  f.value = ''; f.dispatchEvent(new Event('input'));
  // reconcile: mark A's node, drop B, A's node must survive untouched
  items[0].__mark = 1;
  onClosedFrame(null, [A]);
  const after = [...document.querySelectorAll('#closedloglist .cl-item')];
  out.reconciled = after.length === 1 && after[0].__mark === 1;
  return out;
});

// a REAL click on ↩ (trusted event, not element.click())
await page.click('#closedloglist .cl-item:not([hidden]) .cl-back');
await page.waitForTimeout(200);
const r2 = await page.evaluate(() => {
  const out = {};
  out.reopenSent = window.__sent.some(f => f.type === 'reopen' && f.cid === 'aaaa-probe');
  out.modalDown = document.getElementById('closedlog').hidden;
  out.awaitingFocus = pendingNewFocus === true;
  // stand down: no focus is coming (the wire was stubbed)
  pendingNewFocus = false; clearNewFocusWatch(); hideDeadVeil();
  onClosedFrame(null, []);
  hsend = window.__realHsend;
  return out;
});
const r = Object.assign({}, r1, r2);
console.log('CLOSED:', JSON.stringify(r));
const ok = r.modalUp && r.two && r.newestFirst && r.codexTag && r.filtered && r.reconciled && r.reopenSent && r.modalDown && r.awaitingFocus;
console.log(ok ? 'PASS — closed rows newest-first, filter narrows, frames reconcile by node, ↩ sends reopen + closes' : 'FAIL');
await browser.close();
process.exit(ok ? 0 : 1);
