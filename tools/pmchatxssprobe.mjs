// pmchatxssprobe — the PM chat page never treats session text as markup
// (2026-11 audit P5). chat.html built its attention list, event strip, fleet
// badges and tool trace with innerHTML from session-derived strings (title,
// summary, cid…) on a page that can POST /api/tool. A session titled with an
// <img onerror> ran script there. Contract: hostile text shows as text.
//   cd tools && node pmchatxssprobe.mjs
import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
const HERE = dirname(fileURLToPath(import.meta.url)), ROOT = dirname(HERE);
function findChromium(){const c=join(process.env.HOME,'Library/Caches/ms-playwright');
 for(const d of readdirSync(c).filter(d=>d.startsWith('chromium_headless_shell-')).sort().reverse())
  for(const a of ['mac-arm64','mac-x64']){const b=join(c,d,`chrome-headless-shell-${a}`,'chrome-headless-shell');if(existsSync(b))return b;}}
const html = readFileSync(join(ROOT,'controller','chat.html'),'utf8');
const browser = await chromium.launch({ executablePath: findChromium() });
let failed=false; const check=(n,ok,d)=>{console.log(`  ${ok?'✓':'✗'} ${n}${ok||!d?'':' — '+d}`); if(!ok) failed=true;};
const page = await browser.newPage({ viewport:{width:1100,height:800} });
const EVIL = (tag) => `<img src=x onerror="window.__pwned=(window.__pwned||[]).concat('${tag}')">`;
const api = {
  '/api/state': { autonomy:'confirm', model:'m', attention_count:1, harness:{base:'https://pm.probe',token:'',port:0},
                  machines:[{id:EVIL('machine'), connected:true, sessions:2}] },
  '/api/attention': { items:[{ machine:'m1', pid:'p1', cid:EVIL('cid'), sev:'high"><b id=sevbreak>', title:EVIL('title'),
                               summary:EVIL('summary'), suggested_action:EVIL('action') }] },
  '/api/notifications': { events:[{ kind:'blocked', cid:'c1234567', summary:EVIL('event') }] },
  '/api/threads': { threads:[{ id:'t1', title:EVIL('thread'), msgs:1 }], current:'t1' },
  '/api/thread/messages': { messages:[{ who:'bot', text:'hi', trace:[{ tool:EVIL('trace') }] }] },
};
await page.route('https://pm.probe/**', r => {
  const u = new URL(r.request().url());
  if (u.pathname === '/') return r.fulfill({ status:200, contentType:'text/html; charset=utf-8', body: html });
  const body = api[u.pathname];
  return r.fulfill({ status: body ? 200 : 404, contentType:'application/json', body: JSON.stringify(body || {}) });
});
const errors=[]; page.on('pageerror',e=>errors.push(String(e)));
await page.goto('https://pm.probe/', { waitUntil:'domcontentloaded' });
await page.waitForTimeout(1500);
check('the attention row rendered', await page.locator('#att .att').count() === 1);
check('the event strip rendered', await page.locator('#events .badge').count() === 1);
check('no hostile string ran script', !(await page.evaluate(()=>window.__pwned)), JSON.stringify(await page.evaluate(()=>window.__pwned)));
check('no injected <img> anywhere', await page.locator('img[src="x"]').count() === 0, String(await page.locator('img[src="x"]').count()));
check('a hostile severity cannot break out of its attribute', await page.locator('#sevbreak').count() === 0);
check('the title shows as text', (await page.locator('#att .t').innerText()).includes('<img src=x'));
check('the summary shows as text', (await page.locator('#att .s').innerText()).includes('<img src=x'));
check('no page errors', errors.length === 0, errors.join(' | '));
await browser.close();
console.log(failed ? '\nFAIL' : '\nall green');
process.exit(failed ? 1 : 0);
