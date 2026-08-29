import { chromium } from 'playwright-core';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = dirname(HERE);
const cache = join(process.env.HOME, 'Library/Caches/ms-playwright');
const d = readdirSync(cache).filter(x => x.startsWith('chromium_headless_shell-')).sort().reverse()[0];
const exec = ['mac-arm64','mac-x64'].map(a => join(cache, d, `chrome-headless-shell-${a}`, 'chrome-headless-shell')).find(existsSync);
const token = readFileSync(join(ROOT, '.clawd-harness.token'),'utf8').trim();
const b = await chromium.launch({ executablePath: exec });
const p = await b.newPage({ viewport: { width: 1000, height: 700 } });
await p.goto(`http://127.0.0.1:8787/?t=${token}`, { waitUntil: 'networkidle', timeout: 15000 });
await p.waitForTimeout(2000);
const out = await p.evaluate(() => {
  const sent = [];
  window.hsend = f => sent.push(f);                       // intercept: send nothing real
  // a pending restart held by one真 mid-turn session + one background shell
  renderReboot({ type:'restart', pending:true, reason:'server.py changed', busy:2,
    waitedFor: 185, maxWait: 1200,
    blockers:[{cid:'aaaaaaaa-1', title:'Fix the thing', bg:''},
              {cid:'bbbbbbbb-2', title:'Long build', bg:'shell'}] });
  const bar = document.getElementById('rebootbar');
  const now = document.getElementById('rebootNow');
  const msg = document.getElementById('rebootmsg').textContent;
  const titleAttr = now.title;
  const btnShown = !now.hidden;          // read BEFORE the all-clear render below
  now.click();
  // and the all-clear case: nothing mid-turn → no force button
  renderReboot({ type:'restart', pending:true, reason:'x', busy:0, blockers:[], waitedFor:0 });
  return { barShown: !bar.hidden, msg, btnShown, titleAttr, sent,
           hiddenWhenIdle: document.getElementById('rebootNow').hidden };
});
console.log(JSON.stringify(out, null, 1));
await p.screenshot({ path: join(HERE, 'rebootprobe.png') });
await b.close();
const f = [];
if (!out.barShown) f.push('banner not shown');
if (!out.btnShown) f.push('restart-now button missing while blocked');
if (!/Fix the thing/.test(out.msg)) f.push('banner does not name what it waits on');
if (!/held 3m/.test(out.msg)) f.push('banner does not show how long it has been held');
if (!/shell/.test(out.titleAttr)) f.push('button does not warn about background work');
if (!(out.sent[0] && out.sent[0].type === 'restart' && out.sent[0].force === true))
  f.push('button did not send a forcing restart frame: ' + JSON.stringify(out.sent));
if (!out.hiddenWhenIdle) f.push('button still offered when nothing is mid-turn');
if (f.length) { f.forEach(x => console.error('FAIL:', x)); process.exit(1); }
console.log('ok — banner names its blockers; force button sends {restart, force:true}');
