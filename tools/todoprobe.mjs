// todoprobe — guard the ☑ per-iron to-do list (2026-09-23): what's still open
// across an iron, shared by every session in it and every device.
//
//   cd tools && node todoprobe.mjs
//
// What has to hold (fleet mode, iPhone emulation, REAL taps at natural pace):
//   1. IDLE STATE: no overlay anywhere until an iron is scoped; the iron page's
//      inline list is hidden off the iron page
//   2. scoped, the list is UP BY DEFAULT (bottom sheet on touch); the iron
//      row's ☑ (open count) toggles it, remembered per iron; ➤ session seeds
//      the composer with where the list is + the harness-todo pointer
//   3. adding sends an item-level `todo` frame (add, iron, text, the session's
//      projectKey) — never the whole list — and the `todos` snapshot renders it
//   4. repaint, don't rebuild: a snapshot mid-typing keeps the box's text and
//      focus, and an unchanged row is the SAME node
//   5. a real tap on ☐ sends done; the echo strikes it, sinks it, shows the
//      count + `clear done`; tapping the words seeds the composer
//   6. the panel is STICKY across the iron's sessions (a real tab tap) and
//      leaves with the scope; ✕ closes it and the ☑ un-lights
//   7. the sessionless iron page shows the same list inline; adds from there
//      carry no project key
// Desktop: the overlay is a 300px column down the tty's right edge and the 🟦
// block yields that width. Direct mode: `todo` rides hsend, `todos` renders.
// Fake-relay stub as ironprobe/tapprobe — no relay, no session touched.
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

async function newPage(html, url, opts){
  const page = await browser.newPage(opts);
  await page.addInitScript(() => {
    window.__sent=[]; const sockets=[];
    class FakeWS{constructor(u){this.url=u;this.readyState=0;this.binaryType='arraybuffer';sockets.push(this);
      setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen({});},0);}
     send(d){window.__sent.push(d);} close(){this.readyState=3;this.onclose&&this.onclose({});}}
    FakeWS.prototype.addEventListener=function(){}; window.WebSocket=FakeWS;
    window.__relayRx=(o)=>{const s=sockets[sockets.length-1]; if(s&&s.onmessage) s.onmessage({data:JSON.stringify(o)});};
    window.__frames=()=>window.__sent.map(x=>{try{return JSON.parse(x);}catch{return null;}}).filter(Boolean);
    try{localStorage.clear();}catch{}
    for (const m of ['clawd-atg'])
      try{localStorage.setItem('cc_e2e_rs_'+m, JSON.stringify({id:'p-'+m,master:'AAAA',exp:Date.now()+3600e3}));}catch{}
  });
  page.on('pageerror',e=>errors.push(String(e)));
  await page.route(url, r => r.fulfill({status:200,contentType:'text/html; charset=utf-8',body:html}));
  await page.goto(url,{waitUntil:'domcontentloaded',timeout:15000});
  await page.waitForTimeout(500);
  return page;
}
const IRON = {id:'ix1',title:'voice',desc:'all the voice work',tags:[],keys:['github.com/clawdbotatg/gpt-voice','github.com/clawdbotatg/other'],created:1};
const IRON2 = {id:'ix2',title:'quiet',desc:'no sessions here',tags:[],keys:['github.com/clawdbotatg/nosess'],created:2};
async function seedFleet(page){
  await page.evaluate(o=>{ window.__relayRx({type:'prefs',inactive:[],irons:o.irons}); window.__relayRx({type:'todos',todos:{}});
    window.__relayRx({type:'machines',machines:[{id:'clawd-atg',host:'atg',kind:'machine',online:true,lastSeen:0,stats:{projects:3,sessions:2,active:1}}]}); },
    {irons:[IRON,IRON2]});
  await page.waitForTimeout(300);
  await page.evaluate(()=>{
    handleMachineJson('clawd-atg',{type:'projects',projects:[
      {pid:'pa1',name:'gpt-voice',repoUrl:'https://github.com/clawdbotatg/gpt-voice',kind:'gh',status:'ready',sessionCount:1,busyCount:1,waitingCount:0,created:1,pinned:false,lastTouched:100,emoji:'🎙'},
      {pid:'pa2',name:'other',repoUrl:'https://github.com/clawdbotatg/other',kind:'gh',status:'ready',sessionCount:1,busyCount:0,waitingCount:0,created:1,pinned:false,lastTouched:50,emoji:''},
      {pid:'pa3',name:'nosess',repoUrl:'https://github.com/clawdbotatg/nosess',kind:'gh',status:'ready',sessionCount:0,busyCount:0,waitingCount:0,created:1,pinned:false,lastTouched:10,emoji:''}]});
    handleMachineJson('clawd-atg',{type:'sessions',sessions:[
      {cid:'ca1',pid:'pa1',title:'wire the mic',tab:'mic',alive:true,busy:true,pinned:0,promptedAt:Date.now()/1000-60,lastActive:Date.now()/1000},
      {cid:'ca2',pid:'pa2',title:'unrelated job',tab:'other',alive:true,busy:false,pinned:0,promptedAt:Date.now()/1000-120,lastActive:Date.now()/1000}]});
    resolvePendingNav(); navTo('projects');
  });
  await page.waitForTimeout(300);
}
async function tapEl(page, sel){ const b=await page.locator(sel).first().boundingBox(); if(!b) throw new Error('no box for '+sel);
  await page.tap ? page.touchscreen.tap(b.x+b.width/2, b.y+b.height/2) : page.mouse.click(b.x+b.width/2, b.y+b.height/2); await page.waitForTimeout(350); }

// ---- fleet, iPhone -----------------------------------------------------------
const iphone = devices['iPhone 12'];
const page = await newPage(fleetHtml, 'https://fleet.probe/', { ...iphone, viewport:{width:390,height:844} });
await seedFleet(page);
check('isTouch under iPhone emulation', await page.evaluate(()=>isTouch));

// 1. idle state
const idle = await page.evaluate(()=>({ overlay: document.getElementById('irontodo').hidden, inline: document.getElementById('ironvtodo').hidden,
  row: document.getElementById('ironrow').hidden, cls: document.getElementById('left').classList.contains('todoOpen') }));
check('idle: no overlay, no inline list, no scope row', idle.overlay && idle.inline && idle.row && !idle.cls, JSON.stringify(idle));
await page.evaluate(()=>openIron('ix1'));
await page.waitForTimeout(400);
const scoped = await page.evaluate(()=>{ const b=document.getElementById('ironrow').querySelector('.irtodo');
  return { view: currentView(), scope: ironScope, btn: b && b.textContent.trim(), lit: b && b.classList.contains('on'), overlay: document.getElementById('irontodo').hidden }; });
check('scoped: the row carries ☑ (no count yet), lit, and the list is UP BY DEFAULT', scoped.view==='tty' && scoped.scope==='ix1' && scoped.btn==='☑' && scoped.lit && !scoped.overlay, JSON.stringify(scoped));

// 2. a real tap on ☑ closes it, a second one brings it back
await tapEl(page, '#ironrow .irtodo');
check('☑ tap hides the sheet', await page.evaluate(()=>document.getElementById('irontodo').hidden && !todoOpenPref('ix1')));
await tapEl(page, '#ironrow .irtodo');
const opened = await page.evaluate(()=>{ const el=document.getElementById('irontodo'), r=el.getBoundingClientRect(), L=document.getElementById('left').getBoundingClientRect();
  return { hidden: el.hidden, lit: document.getElementById('ironrow').querySelector('.irtodo').classList.contains('on'), pref: todoOpenPref('ix1'),
           sheet: Math.abs(r.bottom-L.bottom)<2 && r.left<2 && r.width>300, title: el.querySelector('.tdtitle').textContent, empty: !el.querySelector('.tdempty').hidden }; });
check('☑ tap opens a bottom sheet, lights the button, remembers the choice', !opened.hidden && opened.lit && opened.pref && opened.sheet && opened.title==='☑ voice' && opened.empty, JSON.stringify(opened));
await page.screenshot({path:join(HERE,'todoprobe-open.png')});   // eyeball frame: the empty sheet over the tty

// 2b. the sheet's grab pill: a real drag up makes it taller, remembered
{ const g = await page.locator('#irontodo .tdgrip').boundingBox(); const h0 = (await page.locator('#irontodo').boundingBox()).height;
  const cx=g.x+g.width/2, cy=g.y+g.height/2;
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:cx,y:cy}]});
  for (let i=1;i<=6;i++) await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:cx,y:cy-i*25}]});
  await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
  await page.waitForTimeout(200);
  const h1 = (await page.locator('#irontodo').boundingBox()).height;
  check('touch: dragging the sheet’s grab pill up makes it ~150px taller, remembered',
        Math.abs((h1-h0)-150) < 12 && await page.evaluate(()=>!!localStorage.getItem('cc_todo_h')), JSON.stringify({h0,h1})); }

// 3. add: real tap into the box, type, Enter
await page.evaluate(()=>{ window.__sent.length=0; });
await tapEl(page, '#irontodo .tdadd input');
await page.keyboard.type('test the mic on the phone', {delay:20});
await page.keyboard.press('Enter');
await page.waitForTimeout(200);
const add = await page.evaluate(()=>{ const f=window.__frames().find(x=>x.type==='todo'); const inp=document.querySelector('#irontodo .tdadd input');
  return { f, cleared: inp.value==='', noPrefs: !window.__frames().some(x=>x.type==='prefs') }; });
check('Enter sends ONE item-level add with the session’s projectKey (never a prefs/list write)',
      add.f && add.f.op==='add' && add.f.iron==='ix1' && add.f.text==='test the mic on the phone' && add.f.key==='github.com/clawdbotatg/gpt-voice' && add.cleared && add.noPrefs, JSON.stringify(add));
const T = {ix1:[{id:'t1',text:'test the mic on the phone',done:false,created:1,doneAt:0,key:'github.com/clawdbotatg/gpt-voice',via:'page'},
               {id:'t2',text:'left by claude at wrap',done:false,created:2,doneAt:0,key:'',via:'agent'}]};
await page.evaluate(t=>window.__relayRx({type:'todos',todos:t}), T);
await page.waitForTimeout(150);
const shown = await page.evaluate(()=>{ const rows=[...document.querySelectorAll('#irontodo .tdi')];
  return { n: rows.length, texts: rows.map(r=>r.querySelector('.tdtext').textContent), proj: rows[0].querySelector('.tdproj').textContent,
           count: document.querySelector('#irontodo .tdcount').textContent, btn: document.querySelector('#ironrow .irtodo').textContent.trim(),
           empty: document.querySelector('#irontodo .tdempty').hidden, foot: document.querySelector('#irontodo .tdfoot').hidden }; });
check('the snapshot renders both rows, project chip, open count, row badge; no clear-done yet',
      shown.n===2 && shown.texts[0]==='test the mic on the phone' && shown.proj==='gpt-voice' && shown.count==='2 open' && shown.btn==='☑ 2' && shown.empty && shown.foot, JSON.stringify(shown));
await page.screenshot({path:join(HERE,'todoprobe-items.png')});  // eyeball frame: two items on the sheet

// 4. repaint, don't rebuild
await tapEl(page, '#irontodo .tdadd input');
await page.keyboard.type('half-typ', {delay:10});
const survive = await page.evaluate(t=>{ const inp=document.querySelector('#irontodo .tdadd input'); const row=document.querySelector('#irontodo .tdi[data-id="t1"]'); row.__mark=1;
  window.__relayRx({type:'todos',todos:t}); window.__relayRx({type:'todos',todos:t});
  const row2=document.querySelector('#irontodo .tdi[data-id="t1"]');
  const out={ focused: document.activeElement===inp, text: inp.value, sameNode: row2 && row2.__mark===1 }; inp.value=''; return out; }, T);
check('a snapshot mid-typing keeps text + focus; an unchanged row is the same node', survive.focused && survive.text==='half-typ' && survive.sameNode, JSON.stringify(survive));

// 5. done by a real tap on ☐, then the echo
await page.evaluate(()=>{ window.__sent.length=0; });
await tapEl(page, '#irontodo .tdi[data-id="t1"] .tdchk');
const doneF = await page.evaluate(()=>window.__frames().find(x=>x.type==='todo'));
check('☐ tap sends done for that id', doneF && doneF.op==='done' && doneF.id==='t1' && doneF.iron==='ix1', JSON.stringify(doneF));
const T2 = {ix1:[{...T.ix1[0],done:true,doneAt:3}, T.ix1[1]]};
await page.evaluate(t=>window.__relayRx({type:'todos',todos:t}), T2);
await page.waitForTimeout(150);
const after = await page.evaluate(()=>{ const rows=[...document.querySelectorAll('#irontodo .tdi')];
  return { order: rows.map(r=>r.dataset.id), done: rows[1].classList.contains('done'), chk: rows[1].querySelector('.tdchk').textContent,
           count: document.querySelector('#irontodo .tdcount').textContent, btn: document.querySelector('#ironrow .irtodo').textContent.trim(),
           foot: document.querySelector('#irontodo .tdfoot').hidden }; });
check('the echo strikes it, sinks it under the open ones, updates counts, shows clear done',
      after.order.join()==='t2,t1' && after.done && after.chk==='☑' && after.count==='1 open · 1 done' && after.btn==='☑ 1' && !after.foot, JSON.stringify(after));
await tapEl(page, '#irontodo .tdi[data-id="t2"] .tdtext');
check('tapping the words seeds the composer', await page.evaluate(()=>document.getElementById('box').value==='left by claude at wrap'));
await tapEl(page, '#irontodo .tdtell');
const tell = await page.evaluate(()=>document.getElementById('box').value);
check('➤ session seeds the composer with where the list is + the harness-todo pointer', /"voice" iron/.test(tell) && /harness-todo add/.test(tell) && /still open/.test(tell), tell);
await page.evaluate(()=>{ box.value=''; box.dispatchEvent(new Event('input',{bubbles:true})); window.__sent.length=0; });
await tapEl(page, '#irontodo .tdfoot button');
check('clear done sends clear', await page.evaluate(()=>{ const f=window.__frames().find(x=>x.type==='todo'); return f && f.op==='clear' && f.iron==='ix1'; }));

// 6. sticky across the iron's sessions (a real tab tap), gone with the scope, ✕
const tabs = await page.evaluate(()=>[...document.querySelectorAll('#sessionbar .stab')].map(t=>t.querySelector('.lbl').textContent));
check('the scoped strip holds both member sessions', tabs.length===2, JSON.stringify(tabs));
const otherTab = await page.evaluate(()=>{ const ts=[...document.querySelectorAll('#sessionbar .stab')]; const i=ts.findIndex(t=>!t.classList.contains('active')); return i; });
await tapEl(page, `#sessionbar .stab:nth-of-type(${otherTab+1})`);
const sticky = await page.evaluate(()=>({ cid: currentCid, scope: ironScope, open: !document.getElementById('irontodo').hidden, view: currentView() }));
check('switching sessions inside the iron keeps the sheet up', sticky.cid==='ca2' && sticky.scope==='ix1' && sticky.open && sticky.view==='tty', JSON.stringify(sticky));
await page.evaluate(()=>navTo('sessions'));
await page.waitForTimeout(200);
const left = await page.evaluate(()=>({ open: !document.getElementById('irontodo').hidden, cls: document.getElementById('left').classList.contains('todoOpen'), pref: todoOpenPref('ix1') }));
check('leaving the scope hides the sheet but keeps the preference', !left.open && !left.cls && left.pref, JSON.stringify(left));
await page.evaluate(()=>openIron('ix1'));
await page.waitForTimeout(300);
check('re-entering the iron brings the sheet straight back', await page.evaluate(()=>!document.getElementById('irontodo').hidden));
await tapEl(page, '#irontodo .tdhead button:not(.tdtell)');
const closed = await page.evaluate(()=>({ open: !document.getElementById('irontodo').hidden, lit: document.querySelector('#ironrow .irtodo').classList.contains('on'), pref: todoOpenPref('ix1') }));
check('✕ closes it and un-lights ☑ (remembered off)', !closed.open && !closed.lit && !closed.pref, JSON.stringify(closed));

// 7. the sessionless iron page: inline list, adds carry no project key
await page.evaluate(()=>{ navTo('projects'); openIron('ix2'); });
await page.waitForTimeout(500);
const inline = await page.evaluate(()=>{ const el=document.getElementById('ironvtodo');
  return { view: currentView(), hidden: el.hidden, title: el.querySelector('.tdtitle') && el.querySelector('.tdtitle').textContent, noX: !el.querySelector('.tdhead button') }; });
check('the iron page shows the list inline (no ✕ — it is part of the page)', inline.view==='iron' && !inline.hidden && inline.title==='☑ quiet' && inline.noX, JSON.stringify(inline));
await page.evaluate(()=>{ window.__sent.length=0; });
await tapEl(page, '#ironvtodo .tdadd input');
await page.keyboard.type('spike the layout', {delay:15});
await page.keyboard.press('Enter');
await page.waitForTimeout(150);
const pageAdd = await page.evaluate(()=>window.__frames().find(x=>x.type==='todo'));
check('an add from the iron page targets that iron with no project key', pageAdd && pageAdd.op==='add' && pageAdd.iron==='ix2' && pageAdd.text==='spike the layout' && pageAdd.key==='', JSON.stringify(pageAdd));
await page.evaluate(()=>window.__relayRx({type:'todos',todos:{ix2:[{id:'q1',text:'spike the layout',done:false,created:1,doneAt:0,key:'',via:'page'}]}}));
await page.waitForTimeout(150);
check('the iron page list renders the echo', await page.evaluate(()=>document.querySelectorAll('#ironvtodo .tdi').length===1));
await page.screenshot({path:join(HERE,'todoprobe-page.png')});   // eyeball frame: the sessionless iron page with its list
await page.evaluate(()=>navTo('irons'));
await page.waitForTimeout(150);
check('off the iron page the inline list is hidden again', await page.evaluate(()=>document.getElementById('ironvtodo').hidden || currentView()!=='iron'));
await page.close();

// ---- fleet, desktop --------------------------------------------------------------
const dpage = await newPage(fleetHtml, 'https://fleet.probe/', { viewport:{width:1100,height:800} });
await seedFleet(dpage);
await dpage.evaluate(t=>window.__relayRx({type:'todos',todos:t}), T2);
await dpage.evaluate(()=>{ openIron('ix1'); });
await dpage.waitForTimeout(400);                       // up by default — no tap needed
const desk = await dpage.evaluate(()=>{ const el=document.getElementById('irontodo'), r=el.getBoundingClientRect(), L=document.getElementById('left').getBoundingClientRect();
  return { hidden: el.hidden, column: Math.abs(r.right-L.right)<2 && Math.abs(r.top-L.top)<2 && Math.abs(r.bottom-L.bottom)<2 && Math.round(r.width)===300,
           tldrRight: getComputedStyle(document.getElementById('tldr')).right, rows: document.querySelectorAll('#irontodo .tdi').length }; });
check('desktop: a 300px column down the tty’s right edge; the 🟦 block yields that width', !desk.hidden && desk.column && desk.tldrRight==='300px' && desk.rows===2, JSON.stringify(desk));
// drag the bar between the tty and the list 120px left → wider panel, remembered; the 🟦 block + corner pills follow
{ const g = await dpage.locator('#irontodo .tdgrip').boundingBox();
  await dpage.mouse.move(g.x+g.width/2, g.y+200); await dpage.mouse.down();
  await dpage.mouse.move(g.x+g.width/2-60, g.y+200, {steps:4}); await dpage.mouse.move(g.x+g.width/2-120, g.y+200, {steps:4}); await dpage.mouse.up();
  await dpage.waitForTimeout(150);
  const w = await dpage.evaluate(()=>({ w: Math.round(document.getElementById('irontodo').getBoundingClientRect().width),
    tldr: getComputedStyle(document.getElementById('tldr')).right, saved: localStorage.getItem('cc_todo_w'),
    pill: getComputedStyle(document.getElementById('closeBtn')).transform }));
  check('dragging the bar widens the list to 420px, the 🟦 block + corner pills follow, size remembered',
        w.w===420 && w.tldr==='420px' && w.saved==='420' && /-420/.test(w.pill), JSON.stringify(w)); }
await dpage.screenshot({path:join(HERE,'todoprobe-desktop.png')});   // eyeball frame: the column beside the tty
await dpage.close();

// ---- direct mode -------------------------------------------------------------------
const xpage = await newPage(raw, 'https://direct.probe/?t=x', { viewport:{width:1000,height:800} });
await xpage.evaluate(()=>{
  window.__relayRx({type:'projects',projects:[
    {pid:'p1',name:'alpha',repoUrl:'https://github.com/x/alpha',kind:'gh',status:'ready',sessionCount:1,busyCount:0,waitingCount:0,created:1,pinned:false,lastTouched:10,emoji:''}],boot:'b1'});
  window.__relayRx({type:'sessions',sessions:[{cid:'c1',pid:'p1',title:'direct job',tab:'job',alive:true,busy:false,pinned:0,promptedAt:1,lastActive:1}],current:null});
  window.__relayRx({type:'irons',irons:[{id:'i9',title:'direct iron',desc:'',tags:[],pids:['p1'],created:1}]});
  window.__relayRx({type:'todos',todos:{i9:[{id:'d1',text:'from the registry',done:false,created:1,doneAt:0,key:'p1',via:'agent'}]}});
});
await xpage.waitForTimeout(300);
await xpage.evaluate(()=>{ openIron('i9'); });
await xpage.waitForTimeout(300);
await xpage.evaluate(()=>{ window.__sent.length=0; });
await xpage.fill('#irontodo .tdadd input', 'direct add');
await xpage.press('#irontodo .tdadd input', 'Enter');
const direct = await xpage.evaluate(()=>{ const f=window.__frames().find(x=>x.type==='todo');
  return { f, rows: document.querySelectorAll('#irontodo .tdi').length, proj: document.querySelector('#irontodo .tdproj').textContent }; });
check('direct: the harness `todos` frame renders (pid → project name) and add rides hsend as a `todo` op',
      direct.rows===1 && direct.proj==='alpha' && direct.f && direct.f.op==='add' && direct.f.iron==='i9' && direct.f.key==='p1', JSON.stringify(direct));
await xpage.close();

check('no page errors', errors.length===0, errors.join(' | '));
await browser.close();
console.log(failed ? 'todoprobe: FAIL' : 'todoprobe: all green');
process.exit(failed ? 1 : 0);
