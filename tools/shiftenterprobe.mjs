// shiftenterprobe — verify Shift+Enter inserts a newline in #box (and does NOT send),
// while plain Enter still submits. Drives the LOCAL running harness (see uiprobe.mjs).
import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { join } from 'node:path';

const ROOT = '/Users/austingriffith/clawd/clawd-harness';
const PORT = '8787';
function findChromium() {
  const cache = join(process.env.HOME, 'Library/Caches/ms-playwright');
  const shells = readdirSync(cache).filter(d => d.startsWith('chromium_headless_shell-')).sort().reverse();
  for (const d of shells) for (const arch of ['mac-arm64','mac-x64']) {
    const bin = join(cache, d, `chrome-headless-shell-${arch}`, 'chrome-headless-shell');
    if (existsSync(bin)) return bin;
  }
}
const token = readFileSync(join(ROOT, '.clawd-harness.token'),'utf8').trim();
const cid = process.argv[2] || '33bf7ec4-0ffc-44f2-8f26-6811c838234a';
const browser = await chromium.launch({ executablePath: findChromium() });

async function probe({ touch }) {
  // touch:true emulates a coarse pointer (phone/iPad) → isTouch in the app is true.
  const ctx = await browser.newContext({ viewport: { width: 1100, height: 800 }, hasTouch: touch, isMobile: touch });
  const page = await ctx.newPage();
  // transcript view (not /tty) so #box is the focused field, not xterm's textarea
  await page.goto(`http://127.0.0.1:${PORT}/?t=${token}#/p/self/s/${cid}`, { waitUntil:'networkidle', timeout:15000 });
  await page.waitForTimeout(2000);
  // Stub sendMessage so a stray Enter can't deliver to a live claude, and count calls.
  await page.evaluate(() => { window.__sent = 0; window.sendMessage = () => { window.__sent++; }; });
  const isTouch = await page.evaluate(() => window.matchMedia('(pointer: coarse)').matches);

  const box = page.locator('#box');
  await box.focus(); await box.fill('');
  await page.keyboard.type('line one');
  await page.keyboard.down('Shift'); await page.keyboard.press('Enter'); await page.keyboard.up('Shift');
  await page.keyboard.type('line two');
  const afterShift = await page.evaluate(() => ({ value: document.getElementById('box').value, sent: window.__sent }));

  // plain Enter: desktop → sends (no newline); touch → inserts a newline (no send)
  await page.keyboard.type('more');
  await page.keyboard.press('Enter');
  const afterEnter = await page.evaluate(() => ({ value: document.getElementById('box').value, sent: window.__sent }));
  await ctx.close();

  const shiftMakesNewline = afterShift.value === 'line one\nline two' && afterShift.sent === 0;
  const plainEnter = touch
    ? (afterEnter.sent === 0 && afterEnter.value.includes('\n'))   // touch: newline, never sends
    : (afterEnter.sent === 1);                                     // desktop: sends
  return { touch, isTouch, shiftMakesNewline, plainEnter, afterShift: afterShift.value, afterEnter: afterEnter.value };
}

const desktop = await probe({ touch: false });
const mobile  = await probe({ touch: true });
console.log(JSON.stringify({ desktop, mobile }, null, 2));
const pass = desktop.shiftMakesNewline && desktop.plainEnter && desktop.isTouch === false
          && mobile.shiftMakesNewline  && mobile.plainEnter  && mobile.isTouch === true;
console.log(pass ? 'PASS' : 'FAIL');
await browser.close();
process.exit(pass ? 0 : 1);
