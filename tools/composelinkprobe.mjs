// composelinkprobe — the compose deep link (2026-09-08):
//   #/p/<key>/new?q=<text>          → text waits in that project's new-session box
//   #/p/<key>/new?q=<text>&send=1   → a session is spawned and the text delivered
// Built for the browser extension ("open a session about this tab"), which knows
// a repo key, never a local pid — so direct mode must resolve the fleet projectKey
// too. Also guards the reload trap: the query must leave the address bar the
// moment it's parsed, or every reload would spawn another session.
//
// Safe: lands on #/p/self (subscribes to nothing), and `hsend` is STUBBED before
// the send=1 case so the `new` frame is captured, never delivered — no real
// session is spawned, nothing reaches the harness.
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
await page.waitForFunction(() => typeof applyCompose === 'function' && projectList.length > 0 && currentView() === 'sessions').catch(() => {});
await page.waitForTimeout(600);

const out = await page.evaluate(async () => {
  const wait = (ms) => new Promise(r => setTimeout(r, ms));
  const r = {};
  localStorage.removeItem('cc_draft:new:self');
  box.value = '';

  // 1. draft form, addressed by the fleet projectKey (what the extension sends)
  const key = projectKey(projectList.find(p => p.pid === 'self'));
  location.hash = '#/p/' + encodeURIComponent(key) + '/new?q=' + encodeURIComponent('hello probe / one');
  await wait(400);
  r.draftBox = box.value;
  r.draftStored = localStorage.getItem('cc_draft:new:self');
  r.strippedHash = location.hash;                      // query gone, pid canonical
  r.pid = currentPid;
  r.view = currentView();

  // 2. send form — hsend stubbed: capture the frame, spawn nothing real
  const frames = [];
  const realHsend = hsend;
  hsend = (f) => { frames.push(f); return true; };
  localStorage.removeItem('cc_draft:new:self');
  box.value = '';
  location.hash = '#/p/self/new?q=' + encodeURIComponent('send probe') + '&send=1';
  await wait(400);
  r.newFrame = frames.find(f => f.type === 'new') || null;
  r.pendingText = pendingSendText;
  r.sendHash = location.hash;
  r.boxAfterSend = box.value;                          // the send path never touches the box
  // stand the fake spawn down
  hsend = realHsend;
  pendingSendText = null; stashPending(null); pendingNewFocus = false;
  localStorage.removeItem('cc_draft:new:self');
  return r;
});
console.log('COMPOSE:', JSON.stringify(out));

const ok = out.draftBox === 'hello probe / one'
        && out.draftStored === 'hello probe / one'
        && out.strippedHash === '#/p/self'
        && out.pid === 'self' && out.view === 'sessions'
        && out.newFrame && out.newFrame.pid === 'self'
        && out.pendingText === 'send probe'
        && !out.sendHash.includes('?')
        && out.boxAfterSend === '';
console.log(ok ? 'PASS — compose link drafts by projectKey, sends by pid, query never survives the parse'
              : 'FAIL');
await browser.close();
process.exit(ok ? 0 : 1);
