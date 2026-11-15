// cardstateprobe — the word on a session card is the dot's own state (2026-11
// audit U5). The card used to work the word out by itself, without `status ===
// 'blocked'` or background work, so a red or magenta dot could sit beside the
// word "idle". Both now come from sessionState().
// Fleet mode + stubbed relay WebSocket + stubbed hsend, FAKE sessions only
// (sessioneventprobe setup) — nothing real is opened or sent to.
//   cd tools && node cardstateprobe.mjs
import { chromium } from 'playwright-core';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
const HERE = dirname(fileURLToPath(import.meta.url)), ROOT = dirname(HERE);
function findChromium(){const c=join(process.env.HOME,'Library/Caches/ms-playwright');
 for(const d of readdirSync(c).filter(d=>d.startsWith('chromium_headless_shell-')).sort().reverse())
  for(const a of ['mac-arm64','mac-x64']){const b=join(c,d,`chrome-headless-shell-${a}`,'chrome-headless-shell');if(existsSync(b))return b;}}
const raw = readFileSync(join(ROOT,'index.html'),'utf8');
const fleetHtml = raw.replace('<head>','<head><script>window.__FLEET__=true;</script>');
const browser = await chromium.launch({ executablePath: findChromium() });
let failed=false; const check=(n,ok,d)=>{console.log(`  ${ok?'✓':'✗'} ${n}${ok||!d?'':' — '+d}`); if(!ok) failed=true;};
const errors=[];
const page = await browser.newPage({ viewport:{width:900,height:800} });
await page.addInitScript(() => {
  window.__sent=[]; const sockets=[];
  class FakeWS{constructor(u){this.url=u;this.readyState=0;this.binaryType='arraybuffer';sockets.push(this);
    setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen({});},0);}
   send(d){window.__sent.push(d);} close(){this.readyState=3;this.onclose&&this.onclose({});}}
  FakeWS.prototype.addEventListener=function(){}; window.WebSocket=FakeWS;
  window.__relayRx=(o)=>{const s=sockets[sockets.length-1]; if(s&&s.onmessage) s.onmessage({data:JSON.stringify(o)});};
  try{localStorage.clear();}catch{}
  try{localStorage.setItem('cc_e2e_rs_clawd-atg', JSON.stringify({id:'p-clawd-atg',master:'AAAA',exp:Date.now()+3600e3}));}catch{}
});
page.on('pageerror',e=>errors.push(String(e)));
page.route('https://fleet.probe/', r=>r.fulfill({status:200,contentType:'text/html; charset=utf-8',body:fleetHtml}));
await page.goto('https://fleet.probe/',{waitUntil:'domcontentloaded'});
await page.waitForTimeout(500);
const now = Date.now()/1000;
const base = { pid:'p1', desc:'', promptCount:2, alive:true, busy:false, autopilot:false, pilotStatus:'', pilotRounds:0, lastActive:now, promptedAt:now };
const SESS = [
  { ...base, cid:'c-blocked', title:'blocked no waiting', status:'blocked' },
  { ...base, cid:'c-bg',      title:'background shell',   bg:'shell' },
  { ...base, cid:'c-wait',    title:'waiting',            waiting:true },
  { ...base, cid:'c-busy',    title:'busy',               busy:true, tool:'Bash' },
  { ...base, cid:'c-dead',    title:'dead',               alive:false },
  { ...base, cid:'c-idle',    title:'idle' },
  { ...base, cid:'c-pin',     title:'pinned blocked',     status:'blocked', pinned: now },
];
await page.evaluate((SESS)=>{ hsend=()=>true; hsendTo=()=>true;
  window.__relayRx({type:'prefs',inactive:[],irons:[]});
  window.__relayRx({type:'machines',machines:[{id:'clawd-atg',host:'atg',kind:'machine',online:true,lastSeen:0,stats:{projects:1,sessions:SESS.length,active:1}}]});
  handleMachineJson('clawd-atg',{type:'projects',projects:[
    {pid:'p1',name:'alpha',repoUrl:'https://github.com/clawdbotatg/alpha',kind:'gh',status:'ready',sessionCount:SESS.length,busyCount:1,waitingCount:1,created:1,pinned:false,lastTouched:100,emoji:''}]});
  handleMachineJson('clawd-atg',{type:'sessions',sessions:SESS});
  location.hash = '#/p/' + encodeURIComponent(projectRows().find(p=>p.name==='alpha').id);
}, SESS);
await page.waitForTimeout(600);
const cards = await page.evaluate(()=>Object.fromEntries([...document.querySelectorAll('#sessions > .scard')].map(c =>
  [c.dataset.cid, { dot: c.querySelector('.sdot').className.replace('sdot','').trim(), meta: c.querySelector('.smeta').textContent }])));
const word = cid => (cards[cid] || {}).meta || '(no card)', dot = cid => (cards[cid] || {}).dot;
check('the sessions rung painted the fake cards', Object.keys(cards).length === 6, JSON.stringify(Object.keys(cards)));
check('red dot (blocked, not waiting) reads "needs you", not "idle"', dot('c-blocked') === 'blocked' && /needs you/.test(word('c-blocked')) && !/idle/.test(word('c-blocked')), dot('c-blocked') + ' | ' + word('c-blocked'));
check('magenta dot (background work) reads "background", not "idle"', dot('c-bg') === 'bgwork' && /background · shell/.test(word('c-bg')) && !/idle/.test(word('c-bg')), dot('c-bg') + ' | ' + word('c-bg'));
check('waiting reads "needs you"', dot('c-wait') === 'blocked' && /needs you/.test(word('c-wait')), word('c-wait'));
check('busy names its tool', dot('c-busy') === 'busy' && /working · Bash/.test(word('c-busy')), word('c-busy'));
check('dead reads "dead"', dot('c-dead') === 'dead' && /dead/.test(word('c-dead')), word('c-dead'));
check('a plain dot still reads "idle"', dot('c-idle') === '' && /· idle$/.test(word('c-idle')), dot('c-idle') + ' | ' + word('c-idle'));
check('the message count is still there', /^2 msg · /.test(word('c-idle')), word('c-idle'));
// the pin board card uses the same word
await page.evaluate(()=>{ location.hash = '#/pins'; });
await page.waitForTimeout(500);
const pin = await page.evaluate(()=>{ const m=[...document.querySelectorAll('.scard .smeta, .pcard .smeta, .smeta')].map(e=>e.textContent).filter(t=>/pinned/.test(t)); return m[0] || ''; });
check('pin board: a blocked pinned session reads "needs you", not "idle"', /needs you/.test(pin) && !/idle/.test(pin), pin || '(no pin card)');
check('no page errors', errors.length === 0, errors.join(' | '));
await browser.close();
console.log(failed ? 'FAIL' : 'PASS — the word on a session card matches its dot');
process.exit(failed ? 1 : 0);
