// tldrgeom — dump a LIVE session's buffer tail + the 🟦 overlay geometry verdict
// from headless Chromium against the local :8787 harness (tldr persisted on).
// Use it when a screenshot says the placement is off; don't guess from the
// picture. Not a gate probe (needs a live pid/cid) — excluded in checkall.sh.
//   cd tools && node tldrgeom.mjs <pid> <cid>
import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { join } from 'node:path';
function findChromium(){const c=join(process.env.HOME,'Library/Caches/ms-playwright');
 for(const d of readdirSync(c).filter(d=>d.startsWith('chromium_headless_shell-')).sort().reverse())
  for(const a of ['mac-arm64','mac-x64']){const b=join(c,d,`chrome-headless-shell-${a}`,'chrome-headless-shell');if(existsSync(b))return b;}}
const [pid, cid] = process.argv.slice(2);
const token = readFileSync('/Users/austingriffith/clawd/clawd-harness/.clawd-harness.token','utf8').trim();
const browser = await chromium.launch({ executablePath: findChromium() });
const page = await browser.newPage({ viewport:{width:1100,height:800}, deviceScaleFactor:2 }); await page.addInitScript(()=>{ try{localStorage.setItem('cc_tldr','1');}catch{} });
await page.goto(`http://127.0.0.1:8787/?t=${token}#/p/${pid}/s/${cid}/tty`, {waitUntil:'domcontentloaded'});
await page.waitForTimeout(4000);
const out = await page.evaluate(()=>{
  const b=term.buffer.active; const lines=[];
  for (let i=Math.max(0,b.length-14); i<b.length; i++) lines.push([i, JSON.stringify(b.getLine(i).translateToString(true))]);
  const rowsEl=term.element.querySelector('.xterm-rows'); const screen=term.element.querySelector('.xterm-screen');
  const termR=document.getElementById('term').getBoundingClientRect(), leftR=document.getElementById('left').getBoundingClientRect();
  const cell=rowsEl.children[0].offsetHeight;
  const lb=tldrLastBuf(); const first=tldrChromeTop()-b.viewportY; const think=first-1;
  const tr = think>=0&&think<rowsEl.children.length ? rowsEl.children[think].getBoundingClientRect() : null;
  const box = tldrEl.getBoundingClientRect();
  const verdict = {thinkText: think>=0 ? rowsEl.children[think].textContent.trim().slice(0,50) : null,
    thinkInsideTerm: tr && tr.top>=termR.top-0.5 && tr.bottom<=termR.bottom+0.5,
    boxTopMinusThinkBottom: tr ? +(box.top-tr.bottom).toFixed(2) : null,
    boxBottomMinusLeftBottom: +(box.bottom-leftR.bottom).toFixed(2), boxH:+box.height.toFixed(2), chromeRows: tldrChromeRows()};
  return {verdict, rows:term.rows, len:b.length, baseY:b.baseY, viewportY:b.viewportY, cell, termTop:termR.top, termBottom:termR.bottom, leftBottom:leftR.bottom,
    screenTop:screen.getBoundingClientRect().top, screenBottom:screen.getBoundingClientRect().bottom, transform:screen.style.transform,
    tldrOn, hold: tldrHold(), trailing: tldrTrailing(), lastBuf: lb, tldrHidden: tldrEl.hidden, tldrTop: tldrEl.style.top, lines};
});
console.log(JSON.stringify(out, null, 1));
await browser.close();
