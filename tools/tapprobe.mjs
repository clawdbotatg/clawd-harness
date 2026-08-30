// tapprobe — RAPID REAL TAPS on an emulated iPhone (fleet mode, stubbed relay).
// Guards two things that shipped broken on 2026-08-26:
//   1. the irons add-project popup: a tap on any row must assign the project
//      and turn the row ✓ in place (toggle checklist, no reshuffle);
//   2. THE GLOBAL TAP-EATER: a JS double-tap-zoom suppressor used to
//      preventDefault() any touchend <300ms after the previous one, cancelling
//      its click — so every quick second tap in the whole app silently did
//      nothing. Zoom is now killed by `touch-action:manipulation` in CSS.
//      The taps below are fired in QUICK succession on purpose: if someone
//      reintroduces a timing-based suppressor, this probe goes red.
//   cd tools && node tapprobe.mjs
import { chromium, devices } from 'playwright-core';
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
const iphone = devices['iPhone 12'];
const page = await browser.newPage({ ...iphone, viewport:{width:390,height:844} });
await page.addInitScript(() => {
  window.__sent=[]; const sockets=[];
  class FakeWS{constructor(u){this.url=u;this.readyState=0;this.binaryType='arraybuffer';sockets.push(this);
    setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen({});},0);}
   send(d){window.__sent.push(d);} close(){this.readyState=3;this.onclose&&this.onclose({});}}
  FakeWS.prototype.addEventListener=function(){}; window.WebSocket=FakeWS;
  window.__relayRx=(o)=>{const s=sockets[sockets.length-1]; if(s&&s.onmessage) s.onmessage({data:JSON.stringify(o)});};
  try{localStorage.clear();}catch{}
  for (const m of ['clawd-atg'])
    try{localStorage.setItem('cc_e2e_rs_'+m, JSON.stringify({id:'p-'+m,master:'AAAA',exp:Date.now()+3600e3}));}catch{}
});
page.on('pageerror',e=>errors.push(String(e)));
await page.route('https://fleet.probe/', r=>r.fulfill({status:200,contentType:'text/html; charset=utf-8',body:fleetHtml}));
await page.goto('https://fleet.probe/',{waitUntil:'domcontentloaded'});
await page.waitForTimeout(500);
await page.evaluate(()=>{ window.__relayRx({type:'prefs',inactive:[],irons:[
  {id:'ix1',title:'slop-computer',desc:'',tags:[],keys:[],created:1}]});
  window.__relayRx({type:'machines',machines:[{id:'clawd-atg',host:'atg',kind:'machine',online:true,lastSeen:0,stats:{projects:3,sessions:0,active:0}}]});
});
await page.waitForTimeout(300);
await page.evaluate(()=>{
  handleMachineJson('clawd-atg',{type:'projects',projects:[
    {pid:'p1',name:'alpha',repoUrl:'https://github.com/clawdbotatg/alpha',kind:'gh',status:'ready',sessionCount:0,busyCount:0,waitingCount:0,created:1,pinned:false,lastTouched:100,emoji:''},
    {pid:'p2',name:'bravo',repoUrl:'https://github.com/clawdbotatg/bravo',kind:'gh',status:'ready',sessionCount:0,busyCount:0,waitingCount:0,created:1,pinned:false,lastTouched:90,emoji:''},
    {pid:'p3',name:'strawmap-data-flow',repoUrl:'https://github.com/clawdbotatg/strawmap-data-flow',kind:'gh',status:'ready',sessionCount:0,busyCount:0,waitingCount:0,created:1,pinned:false,lastTouched:80,emoji:''}]});
  handleMachineJson('clawd-atg',{type:'sessions',sessions:[]});
  resolvePendingNav();
  openIron('ix1');
});
await page.waitForTimeout(400);
check('isTouch is on under iPhone emulation', await page.evaluate(()=>isTouch));

// A MEMBER-less iron lands on the iron page (#ironview) with the add-project
// picker auto-opened over it — adding a project is the only next step there.
check('empty iron auto-opens the popup', await page.evaluate(()=>getComputedStyle(document.getElementById('ironaddmodal')).display!=='none'));

// TAP THE THIRD ROW (index 2 ≠ ironAddSel 0) — the production gesture
await page.evaluate(()=>{ window.__sent.length=0; });
const rows = await page.$$('#ironaddlist button');
check('popup lists the 3 projects', rows.length===3, String(rows.length));
if (rows[2]) await rows[2].tap();
await page.waitForTimeout(120);
const after1 = await page.evaluate(()=>({
  frames: window.__sent.map(x=>{try{return JSON.parse(x);}catch{return null;}}).filter(Boolean).filter(x=>x.type==='prefs').length,
  keys: (ironList[0].keys||[]).slice() }));
check('FIRST tap on a non-first row assigns the project', after1.frames>=1 && after1.keys.length===1, JSON.stringify(after1));


// tap another row (first row this time)
await page.evaluate(()=>{ window.__sent.length=0; });
const rows2 = await page.$$('#ironaddlist button');
if (rows2[0]) await rows2[0].tap();
await page.waitForTimeout(200);
const after2 = await page.evaluate(()=>({
  frames: window.__sent.map(x=>{try{return JSON.parse(x);}catch{return null;}}).filter(Boolean).filter(x=>x.type==='prefs').length,
  keys: (ironList[0].keys||[]).slice() }));
check('tap on the first row also assigns', after2.frames>=1 && after2.keys.length===2, JSON.stringify(after2));

const ticks = await page.evaluate(()=>[...document.querySelectorAll('#ironaddlist button')]
  .filter(b=>b.classList.contains('inx')).length);
check('both tapped rows wear the ✓ in place', ticks===2, String(ticks));

await page.screenshot({path:join(HERE,'tapprobe.png')});
check('no page errors', errors.length===0, errors.join(' | '));
await browser.close();
console.log(failed?'FAIL':'PASS'); process.exit(failed?1:0);
