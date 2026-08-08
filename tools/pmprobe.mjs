// pmprobe — drive the PM tab in a real browser while a turn is "thinking",
// and assert the controls still work.
//
//   cd tools && node pmprobe.mjs
//
// The bug this pins down (2026-08-08): every PM thread endpoint ran under the
// controller's turn lock, so while the PM was thinking — minutes — ＋ new, the
// thread tabs and ✕ archive all HUNG. The tab read as completely frozen, and
// each dead click also burned one of the browser's ~6 connections to the origin
// until the rest of the page stalled too. See controller/test_pm_responsive.py
// for the server half; this is the half that proves it from the user's seat.
//
// Needs no live controller: every /pm/* call is intercepted here, with /api/chat
// held open to simulate a turn in flight. It only ever talks to the PM surface,
// so it never touches a real session.
//
// Exit code is non-zero if a check fails — so it works in a verify flow.

import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = dirname(HERE);
const PORT = process.env.HARNESS_PORT || '8787';

function findChromium() {
  const cache = join(process.env.HOME, 'Library/Caches/ms-playwright');
  if (!existsSync(cache)) return null;
  const shells = readdirSync(cache).filter(d => d.startsWith('chromium_headless_shell-')).sort().reverse();
  for (const d of shells) {
    for (const arch of ['mac-arm64', 'mac-x64']) {
      const bin = join(cache, d, `chrome-headless-shell-${arch}`, 'chrome-headless-shell');
      if (existsSync(bin)) return bin;
    }
  }
  return null;
}

const exec = findChromium();
if (!exec) { console.error('No cached playwright chromium found. Run: cd tools && npx playwright install chromium'); process.exit(2); }

let token = '';
try { token = readFileSync(join(ROOT, '.clawd-harness.token'), 'utf8').trim(); } catch {}

const browser = await chromium.launch({ executablePath: exec });
// serviceWorkers: 'block' — a fresh profile registers sw.js on load, and the
// app's controllerchange handler then location.reload()s the page mid-probe,
// destroying the execution context under whatever check runs next.
const page = await browser.newPage({ viewport: { width: 1100, height: 800 }, serviceWorkers: 'block' });

// -- fake controller ---------------------------------------------------------
// Two threads; a chat request that never resolves until we let it go.
let threads = [{ id: 't1', title: 'alpha', desc: 'probing the pm tab', archived: false, count: 1, msgs: 2, current: true },
               { id: 't2', title: 'beta', archived: false, count: 0, msgs: 0, current: false }];
let current = 't1';
const msgs = { t1: [{ who: 'me', text: 'hello alpha' }, { who: 'bot', text: 'hi from alpha', trace: [] }],
               t2: [] };
let releaseTurn = null;
const turnHeld = new Promise(res => { releaseTurn = res; });
let turnStarted = null;
const turnHasStarted = new Promise(res => { turnStarted = res; });
const json = (route, body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });

await page.route('**/pm/**', async route => {
  const url = new URL(route.request().url());
  const p = url.pathname.replace(/^\/pm/, '');
  const post = () => { try { return JSON.parse(route.request().postData() || '{}'); } catch { return {}; } };
  const summary = () => ({ threads, current, archived_count: threads.filter(t => t.archived).length });

  if (p === '/api/state') return json(route, { autonomy: 'auto', backend: 'bankr', model: 'x',
                                               models: ['x'], machines: [], attention_count: 0,
                                               harness: { base: '', token: '', port: 8787 } });
  if (p === '/api/threads') return json(route, summary());
  if (p === '/api/thread/messages') return json(route, { messages: msgs[current] || [] });
  if (p === '/api/chat') {                       // the "thinking" turn
    // Faithful to Router.chat: pin the thread up front and record the user turn
    // BEFORE the brain runs, so `current` moving mid-turn can't misroute either.
    const tid = current;
    msgs[tid] = [...(msgs[tid] || []), { who: 'me', text: post().message || '' }];
    threads = threads.map(t => t.id === tid ? { ...t, msgs: msgs[tid].length } : t);
    turnStarted();
    await turnHeld;                              // ...held open until we release it
    msgs[tid] = [...msgs[tid], { who: 'bot', text: 'the reply', trace: [] }];
    threads = threads.map(t => t.id === tid ? { ...t, msgs: msgs[tid].length } : t);
    return json(route, { reply: 'the reply', trace: [] });
  }
  if (p === '/api/thread/new') {
    const id = 't' + (threads.length + 1);
    threads = [...threads.map(t => ({ ...t, current: false })),
               { id, title: 'New thread', archived: false, count: 0, msgs: 0, current: true }];
    current = id; msgs[id] = [];
    return json(route, summary());
  }
  if (p === '/api/thread/select') {
    current = post().id;
    threads = threads.map(t => ({ ...t, current: t.id === current, archived: t.id === current ? false : t.archived }));
    return json(route, { ok: true, ...summary() });
  }
  if (p === '/api/thread/archive') {
    const id = post().id;
    threads = threads.map(t => t.id === id ? { ...t, archived: true } : t);
    if (current === id) { const live = threads.filter(t => !t.archived); current = live.length ? live[0].id : current; }
    threads = threads.map(t => ({ ...t, current: t.id === current }));
    return json(route, summary());
  }
  return json(route, {});
});

const errors = [];
page.on('pageerror', e => errors.push(String(e)));

let failed = false;
const check = (name, ok, detail) => {
  console.log(`  ${ok ? '✓' : '✗'} ${name}${ok || !detail ? '' : ' — ' + detail}`);
  if (!ok) failed = true;
};

try {
  await page.goto(`http://127.0.0.1:${PORT}/?t=${token}#/pm`, { waitUntil: 'networkidle', timeout: 15000 });
} catch (e) {
  console.error(`Could not load the harness on :${PORT} — is server.py running?  (${e.message})`);
  await browser.close(); process.exit(2);
}
await page.waitForTimeout(1200);

const tabs = () => page.$$eval('#sessionbar .pmtab',
  els => els.map(e => ({ label: e.querySelector('.lbl')?.textContent || '',
                         active: e.classList.contains('active'),
                         busy: !!e.querySelector('.sdot.busy') })));
const ctlByText = (t) => page.locator('#sessionbar .pmctl', { hasText: t }).first();

check('PM view opened with its thread tabs', (await tabs()).length === 2, JSON.stringify(await tabs()));

// -- session parity: the running AI tldr line + quick-prompt chips ------------
const descText = await page.$eval('#sessiondesc', e => e.innerText).catch(() => '');
check('the AI title + running tldr line shows for the current thread',
      /alpha/.test(descText) && /probing the pm tab/.test(descText), descText);
check('the quick-prompt chips are visible in the PM view',
      await page.$eval('#quickchips', e => !!e.offsetParent && e.children.length > 0));

// -- fire a prompt; the turn now hangs, exactly like a real thinking PM --------
await page.fill('#box', 'do a long thing');
await page.click('#send').catch(async () => { await page.press('#box', 'Enter'); });
await turnHasStarted;
await page.waitForTimeout(400);

const thinking = await page.$('#pmfeed .thinking');
check('the thinking spinner is up (a turn really is in flight)', !!thinking);

const busyTab = (await tabs()).find(t => t.busy);
check('the thinking thread wears the busy LED', !!busyTab && busyTab.label === 'alpha',
      JSON.stringify(await tabs()));

// -- THE REGRESSION: can we still drive the tab while it thinks? --------------
const clickWithin = async (name, fn, ms = 2500) => {
  const t0 = Date.now();
  try { await Promise.race([fn(), new Promise((_, rej) => setTimeout(() => rej(new Error('timed out')), ms))]); }
  catch (e) { check(name, false, e.message); return null; }
  return Date.now() - t0;
};

let ms = await clickWithin('switching to another thread works mid-turn',
  async () => { await page.click('#sessionbar .pmtab:nth-of-type(2)'); await page.waitForTimeout(300); });
if (ms !== null) {
  const now = await tabs();
  check('  …and that thread is the active one', !!now.find(t => t.label === 'beta' && t.active), JSON.stringify(now));
  check('  …while alpha keeps its busy LED', !!now.find(t => t.label === 'alpha' && t.busy), JSON.stringify(now));
  check('  …and beta shows no spinner (it is not the thinking thread)',
        !(await page.$('#pmfeed .thinking')));
}

ms = await clickWithin('＋ new thread works mid-turn',
  async () => { await ctlByText('new').click(); await page.waitForTimeout(300); });
if (ms !== null) check('  …and the new thread appeared', (await tabs()).length === 3, JSON.stringify(await tabs()));

// -- let the turn land; it must go to ITS thread, not the one we're viewing ---
releaseTurn();
await page.waitForTimeout(900);
const feedNow = await page.$eval('#pmfeed', e => e.innerText);
check('the reply did NOT leak into the thread we switched to',
      !/the reply/.test(feedNow), feedNow.replace(/\s+/g, ' ').slice(0, 120));
check('no busy LED is left behind once the turn ends',
      !(await tabs()).some(t => t.busy), JSON.stringify(await tabs()));

// switching back to alpha shows the reply that landed while we were away
await page.click('#sessionbar .pmtab:nth-of-type(1)');
await page.waitForTimeout(700);
const alphaFeed = await page.$eval('#pmfeed', e => e.innerText);
check('coming back to the thinking thread shows the reply it received',
      /the reply/.test(alphaFeed), alphaFeed.replace(/\s+/g, ' ').slice(0, 120));

// -- a quick-prompt chip sends into the PM thread (turnHeld is resolved now,
//    so this chat answers immediately) ----------------------------------------
await page.click('#quickchips button:first-child');   // the 'tldr' chip
await page.waitForTimeout(700);
const chipFeed = await page.$eval('#pmfeed', e => e.innerText);
check('a chip tap sends its prompt into the PM chat',
      /tldr/.test(chipFeed), chipFeed.replace(/\s+/g, ' ').slice(-120));

// -- swipe left/right hops between PM threads (same gesture as the session rail) --
// Synthesize the touch sequence the shared #app swipe handler listens for; it
// routes through navFwd/navBack → cyclePmThread, so this proves the whole chain.
const swipe = (dx) => page.evaluate((dx) => {
  const el = document.getElementById('app');
  const mk = (type, x) => {
    const t = new Touch({ identifier: 1, target: el, clientX: x, clientY: 300 });
    return new TouchEvent(type, { touches: type === 'touchend' ? [] : [t],
                                  changedTouches: [t], bubbles: true, cancelable: true });
  };
  el.dispatchEvent(mk('touchstart', 300));
  el.dispatchEvent(mk('touchmove', 300 + dx / 2));
  el.dispatchEvent(mk('touchend', 300 + dx));
}, dx);
const activeTab = async () => (await tabs()).find(t => t.active)?.label;

const beforeSwipe = await activeTab();          // 'alpha' (we just switched back)
await swipe(-140);                              // swipe left = next thread
await page.waitForTimeout(500);
const afterLeft = await activeTab();
check('swipe left moves to the next PM thread', afterLeft !== beforeSwipe && !!afterLeft,
      `active: ${beforeSwipe} -> ${afterLeft}`);
check('  …and we are still on the PM view (no depth nav)', /#\/pm/.test(page.url()), page.url());
await swipe(140);                               // swipe right = previous thread
await page.waitForTimeout(500);
const afterRight = await activeTab();
check('swipe right comes back to the previous thread', afterRight === beforeSwipe,
      `active: ${afterLeft} -> ${afterRight}`);

check('no uncaught page errors', errors.length === 0, errors.join(' | '));

const shot = join(HERE, 'pmprobe.png');
await page.screenshot({ path: shot, fullPage: false });
console.log('\nscreenshot ->', shot);
await browser.close();
if (failed) { console.error('FAILED: the PM tab is not fully usable during a turn.'); process.exit(1); }
console.log('PASSED: PM controls stay live while a turn is thinking.');
