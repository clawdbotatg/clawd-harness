// ironprobe — guard the 🔥 irons layer (2026-08-26): named groups of projects,
// a rung above the projects list.
//
//   cd tools && node ironprobe.mjs
//
// What has to hold (fleet mode, the production surface):
//   1. the 🔥 header icon sits immediately LEFT of the 🗂️ projects icon and opens #/irons
//   2. creating an iron sends an IRONS-ONLY prefs frame (never `inactive` —
//      clobbering the machines deny-list from the irons page would be a
//      passkey-storm regression), and the relay echo is applied
//   3. a project card's 🔥 corner button opens the picker; assigning stores the
//      cross-machine projectKey and badges the card
//   4. the iron page shows title/desc/tags + EVERY session from EVERY member
//      project across machines — 📌 pinned members at the END, marked
//   5. the ONE box (filter + create-name, a live <input> — there is NO separate
//      create form) survives the repaint a frame triggers: focus + un-mirrored
//      text intact (the projects-rung rule); ＋ create names the iron from it
//   6. deep link #/i/<id> lands on the iron page
//   7. the list is a PRIORITY order: 🔎 filter box (arrival-focused, Enter on a
//      lone match dives in, survives repaints) + ⠿ drag-to-reorder that
//      persists (new irons land on top; no date sort)
// And in DIRECT mode: the harness `irons` frame renders, assignment goes
// out as an ironAssign op (registry-backed), and reorder as ironOrder.
//
// Same fake-relay stub as fleetprobe/settingsprobe — window.WebSocket is
// replaced before the page loads, machine frames are injected straight into
// handleMachineJson, so no relay, no worker, no passkey, and no session is
// ever touched. Non-zero exit on any failure.
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

async function newPage(html, url){
  const page = await browser.newPage({ viewport: { width: 1000, height: 900 } });
  await page.addInitScript(() => {
    window.__sent=[]; const sockets=[];
    class FakeWS{constructor(u){this.url=u;this.readyState=0;this.binaryType='arraybuffer';sockets.push(this);
      setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen({});},0);}
     send(d){window.__sent.push(d);} close(){this.readyState=3;this.onclose&&this.onclose({});}}
    FakeWS.prototype.addEventListener=function(){}; window.WebSocket=FakeWS;
    window.__relayRx=(o)=>{const s=sockets[sockets.length-1]; if(s&&s.onmessage) s.onmessage({data:JSON.stringify(o)});};
    try{localStorage.clear();}catch{}
    for (const m of ['clawd-atg','clawd-head'])
      try{localStorage.setItem('cc_e2e_rs_'+m, JSON.stringify({id:'p-'+m,master:'AAAA',exp:Date.now()+3600e3}));}catch{}
  });
  page.on('pageerror',e=>{ errors.push(String(e)); });
  await page.route(url, r => r.fulfill({status:200,contentType:'text/html; charset=utf-8',body:html}));
  await page.goto(url,{waitUntil:'domcontentloaded',timeout:15000});
  await page.waitForTimeout(500);
  return page;
}
const errors=[];

// ---- fleet mode -------------------------------------------------------------
const page = await newPage(fleetHtml, 'https://fleet.probe/');
await page.evaluate(o=>window.__relayRx(o),{type:'prefs',inactive:[],irons:[]});
await page.evaluate(o=>window.__relayRx(o),{type:'machines',machines:[
  {id:'clawd-atg',host:'atg',kind:'machine',online:true,lastSeen:0,stats:{projects:2,sessions:2,active:1}},
  {id:'clawd-head',host:'head',kind:'machine',online:true,lastSeen:0,stats:{projects:1,sessions:1,active:0}}]});
await page.waitForTimeout(400);
// two machines; the same gh repo lives on both (one cross-machine projectKey),
// plus a second repo on atg only
await page.evaluate(()=>{
  handleMachineJson('clawd-atg',{type:'projects',projects:[
    {pid:'pa1',name:'gpt-voice',repoUrl:'https://github.com/clawdbotatg/gpt-voice',kind:'gh',status:'ready',sessionCount:1,busyCount:1,waitingCount:0,created:1,pinned:false,lastTouched:100,emoji:'🎙'},
    {pid:'pa2',name:'other',repoUrl:'https://github.com/clawdbotatg/other',kind:'gh',status:'ready',sessionCount:1,busyCount:0,waitingCount:0,created:1,pinned:false,lastTouched:50,emoji:''}]});
  handleMachineJson('clawd-head',{type:'projects',projects:[
    {pid:'ph1',name:'gpt-voice',repoUrl:'https://github.com/clawdbotatg/gpt-voice',kind:'gh',status:'ready',sessionCount:1,busyCount:0,waitingCount:0,created:1,pinned:false,lastTouched:90,emoji:'🎙'}]});
  handleMachineJson('clawd-atg',{type:'sessions',sessions:[
    {cid:'ca1',pid:'pa1',title:'wire the mic',tab:'mic',alive:true,busy:true,pinned:0,promptedAt:Date.now()/1000-60,lastActive:Date.now()/1000},
    {cid:'ca2',pid:'pa2',title:'unrelated job',tab:'other',alive:true,busy:false,pinned:0,promptedAt:Date.now()/1000-120,lastActive:Date.now()/1000}]});
  handleMachineJson('clawd-head',{type:'sessions',sessions:[
    {cid:'ch1',pid:'ph1',title:'parked hud idea',tab:'hud',alive:true,busy:false,pinned:Date.now()/1000-300,promptedAt:Date.now()/1000-900,lastActive:Date.now()/1000}]});
  resolvePendingNav();   // the next roster heartbeat would do this — clears the boot pendingNav so syncUrl runs
  navTo('projects');
});
await page.waitForTimeout(400);

// 1. the 🔥 header icon, immediately LEFT of the 🗂️ projects icon
const head = await page.evaluate(()=>{
  const b=document.getElementById('ironsBtn');
  if(!b) return null;
  return { emoji:b.textContent.trim(), isHbtn:b.classList.contains('hbtn'),
           nextIsProjects: b.nextElementSibling && b.nextElementSibling.id==='projectsBtn' };
});
check('🔥 header icon sits left of the 🗂️ projects icon',
      !!head && head.emoji==='🔥' && head.isHbtn && head.nextIsProjects, JSON.stringify(head));
await page.evaluate(()=>document.getElementById('ironsBtn').click());
await page.waitForTimeout(300);
check('🔥 opens its own page at #/irons', await page.evaluate(()=>currentView()==='irons' && location.hash==='#/irons'));

// 2. create an iron: the ONE box is both filter and create-name — type a name,
// hit ＋ create. (TITLE ONLY — desc is haiku's job, tags come later via ✎.)
check('no second create UI — one box + ＋ create, edit overlay hidden until asked',
      await page.evaluate(()=>!document.getElementById('ironform') && !document.getElementById('ironAddBtn')
        && document.querySelectorAll('#ironfilterrow input').length===1
        && !!document.getElementById('ironCreateBtn')
        && getComputedStyle(document.getElementById('ironedit')).display==='none'));
await page.evaluate(()=>{ window.__sent.length=0;
  const inp=document.getElementById('ironFilterInp');
  inp.value='voice'; inp.dispatchEvent(new Event('input',{bubbles:true}));
  document.getElementById('ironCreateBtn').click();
});
await page.waitForTimeout(200);
check('＋ create clears the box (back to filtering everything)',
      await page.evaluate(()=>document.getElementById('ironFilterInp').value===''));
const wrote = await page.evaluate(()=>{
  const f=window.__sent.map(x=>{try{return JSON.parse(x);}catch{return null;}}).filter(Boolean)
          .find(x=>x.type==='prefs');
  return f?{hasIrons:!!f.irons,title:f.irons&&f.irons[0]&&f.irons[0].title,leaksInactive:'inactive' in f}:null;
});
check('creating sends an IRONS-ONLY prefs frame', !!wrote && wrote.hasIrons && wrote.title==='voice' && !wrote.leaksInactive, JSON.stringify(wrote));
const ironId = await page.evaluate(()=>ironList[0].id);
// the relay's echo is a faithful copy of the write (it confirms + clears the
// dirty flag); only THEN can a different write from elsewhere land
await page.evaluate(()=>window.__relayRx({type:'prefs',inactive:[],irons:JSON.parse(JSON.stringify(ironList))}));
await page.evaluate(o=>window.__relayRx(o),{type:'prefs',inactive:[],
  irons:[{id:ironId,title:'voice',desc:'all the voice work',tags:['speech','hud'],keys:[],created:1}]});
await page.waitForTimeout(200);
check('the relay echo lands (list shows the iron)',
      await page.evaluate(()=>/voice/.test(document.getElementById('ironlist').innerText)));

// 5. repaint survival: un-mirrored text + focus in the one box
const form = await page.evaluate(()=>{
  const t=document.getElementById('ironFilterInp');
  t.focus(); t.value='half-typ';                          // typed but not yet mirrored (no input event)
  renderIronList();                                       // literally what a frame does
  const out={ focused: document.activeElement===t, text: t.value };
  t.value=''; t.dispatchEvent(new Event('input',{bubbles:true}));
  return out;
});
check('the box survives a repaint (focus + un-mirrored text)', form.focused && form.text==='half-typ', JSON.stringify(form));

// 2b. an EMPTY iron has no session to land on — tapping it opens the
// ＋ add-project picker over the irons list instead of any intermediate page
const empty = await page.evaluate(()=>{
  openIron(ironList[0].id);
  const modal=document.getElementById('ironaddmodal');
  const out={ view: currentView(), picker: getComputedStyle(modal).display!=='none' };
  closeIronAdd();
  return out;
});
check('tapping an EMPTY iron stays on the list + opens the add-project picker',
      empty.view==='irons' && empty.picker, JSON.stringify(empty));

// 3. assign from a project card via the picker
await page.evaluate(()=>navTo('projects'));
await page.waitForTimeout(300);
const pick = await page.evaluate(()=>{
  const card=[...document.querySelectorAll('#projcards .scard')].find(c=>/gpt-voice/.test(c.innerText));
  if(!card) return null;
  card.querySelector('.sfire').click();
  const modal=document.getElementById('ironpickmodal');
  const open=!modal.hidden;
  const btn=[...document.querySelectorAll('#ironpicklist button')].find(b=>/voice/.test(b.textContent));
  if(btn) btn.click();
  return { open, closed: modal.hidden, keys: ironList[0].keys.slice() };
});
check('🔥 corner button opens the picker; assigning stores the projectKey',
      !!pick && pick.open && pick.closed && pick.keys.length===1 && /gpt-voice/.test(pick.keys[0]), JSON.stringify(pick));
await page.waitForTimeout(200);
check('the card now wears the iron badge',
      await page.evaluate(()=>{ renderProjectRung();
        const card=[...document.querySelectorAll('#projcards .scard')].find(c=>/gpt-voice/.test(c.innerText));
        return !!card && /🔥 voice/.test(card.innerText); }));

// 4. opening an iron = STRAIGHT into its session plane: one-row chrome between
// the top bar and the strip, warmest session's tty under it — no detail page
await page.evaluate(id=>openIron(id), ironId);
await page.waitForTimeout(300);
const detail = await page.evaluate(()=>{
  const row=document.getElementById('ironrow');
  const tabs=[...document.querySelectorAll('#sessionbar .stab')];
  return { view: currentView(), scope: ironScope, cid: currentCid, hash:location.hash,
           rowShown: !row.hidden, rowText: row.innerText,
           barShown: !document.getElementById('sessionbar').hidden,
           composerShown: getComputedStyle(document.querySelector('footer .inputrow')).display!=='none',
           tabs:tabs.map(t=>({lbl:t.querySelector('.lbl').textContent, parked:t.classList.contains('parked'),
                              active:t.classList.contains('active')})) };
});
check('tapping an iron dives STRAIGHT into its warmest session (no intermediate page)',
      detail.view==='tty' && detail.scope===ironId && detail.cid==='ca1'
      && detail.hash==='#/i/'+ironId+'/s/ca1/tty' && detail.composerShown, JSON.stringify(detail));
check('the one-row chrome shows title + desc + member chip + ✎ 🗑',
      detail.rowShown && /🔥 voice/.test(detail.rowText) && /all the voice work/.test(detail.rowText)
      && /gpt-voice/.test(detail.rowText) && /✎/.test(detail.rowText) && /🗑/.test(detail.rowText),
      JSON.stringify(detail.rowText));
check('sessions from BOTH machines of the member project ride the strip',
      detail.barShown && detail.tabs.length===2
      && detail.tabs.some(t=>/mic/.test(t.lbl)) && detail.tabs.some(t=>/hud/.test(t.lbl))
      && detail.tabs[0].active,
      JSON.stringify(detail.tabs));
check('📌 pinned member sits at the END, marked',
      detail.tabs.length===2 && detail.tabs[1].parked && /^📌 /.test(detail.tabs[1].lbl) && !detail.tabs[0].parked,
      JSON.stringify(detail.tabs));
check('the unrelated project’s session stays out', !detail.tabs.some(t=>/other/.test(t.lbl)));
// the AI desc: a reply from ironDescribe is stored (prefs push) + rendered in the row
const descFlow = await page.evaluate(()=>{
  window.__sent.length=0;
  onIronDesc({ id: ironList[0].id, desc: 'haiku wrote this' });
  const f=window.__sent.map(x=>{try{return JSON.parse(x);}catch{return null;}}).filter(Boolean)
          .find(x=>x.type==='prefs');
  return { desc: ironList[0].desc,
           pushed: !!(f && f.irons && f.irons[0].desc==='haiku wrote this' && !('inactive' in f)),
           shown: document.getElementById('ironrow').innerText.includes('haiku wrote this') };
});
check('haiku’s desc is stored (irons-only push) + rendered in the row',
      descFlow.desc==='haiku wrote this' && descFlow.pushed && descFlow.shown, JSON.stringify(descFlow));
// ✎ edits the iron in place (overlay — no page to go to)
const edit = await page.evaluate(()=>{
  [...document.querySelectorAll('#ironrow button')].find(b=>b.textContent==='✎').click();
  const el=document.getElementById('ironedit');
  const out={ open: !el.hidden, title: document.getElementById('ironEditTitle').value,
              view: currentView() };
  document.getElementById('ironEditCancel').click();
  out.closed = el.hidden;
  return out;
});
check('✎ opens the edit overlay IN PLACE (view stays tty), prefilled, cancel closes',
      edit.open && edit.title==='voice' && edit.view==='tty' && edit.closed, JSON.stringify(edit));

// 5b. the ＋ add-project picker on the iron page: a toggle checklist — the row
// you tap turns ✓ in place (never vanishes/reshuffles), and the write survives
// an unauthed socket (the 2026-08-26 production loss: mutate locally, frame
// silently dropped, next prefs frame reverts).
const addFlow = await page.evaluate(()=>{
  const modal=document.getElementById('ironaddmodal');
  const idle=getComputedStyle(modal).display==='none';       // invisible until asked
  const chip=document.querySelector('#ironrow .iprojchip.iadd');
  if(!chip) return { idle, chip:false };
  chip.click();
  const open=getComputedStyle(modal).display!=='none';
  const inp=document.getElementById('ironaddinput');
  const focused=document.activeElement===inp;
  inp.value='oth'; inp.dispatchEvent(new Event('input',{bubbles:true}));
  const rows=[...document.querySelectorAll('#ironaddlist button')].map(b=>b.textContent);
  window.__sent.length=0;
  inp.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
  const f=window.__sent.map(x=>{try{return JSON.parse(x);}catch{return null;}}).filter(Boolean)
          .find(x=>x.type==='prefs');
  const keys=(f && f.irons && f.irons[0] && f.irons[0].keys)||[];
  const stillOpen=!modal.hidden;
  const ticked=[...document.querySelectorAll('#ironaddlist button')]
               .filter(b=>b.classList.contains('inx')).map(b=>b.textContent);
  // no hover handlers on the rows — a mousemove re-render eats the first iOS tap
  const hoverFree=[...document.querySelectorAll('#ironaddlist button')].every(b=>!b.onmousemove);
  document.getElementById('ironaddclose').click();
  return { idle, chip:true, open, focused, rows, keys, leaks: !!(f && 'inactive' in f),
           stillOpen, closed: modal.hidden, ticked, hoverFree };
});
check('＋ add-project picker: hidden until asked, the chip opens it focused',
      !!addFlow && addFlow.idle && addFlow.chip && addFlow.open && addFlow.focused, JSON.stringify(addFlow));
check('typing filters to the one matching project',
      addFlow.rows && addFlow.rows.length===1 && /other/.test(addFlow.rows[0]), JSON.stringify(addFlow.rows));
check('Enter toggles it IN (irons-only prefs frame), popup stays open, row turns ✓ in place',
      addFlow.keys.length===2 && addFlow.keys.some(k=>/\/other$/.test(k)) && !addFlow.leaks
      && addFlow.stillOpen && addFlow.closed
      && addFlow.ticked.length===1 && /✓ .*other/.test(addFlow.ticked[0]) && addFlow.hoverFree,
      JSON.stringify(addFlow));
// the unauthed-socket hole: an edit made before auth completes must QUEUE and
// survive the stale prefs frame the relay sends on connect — never vanish
const unauthed = await page.evaluate(()=>{
  ironAssign(projectRows().find(p=>/other/.test(p.name)).id, '');   // back out (authed write)
  const before = ironList[0].keys.slice();
  relayAuthed=false; relayOutbox.length=0; window.__sent.length=0;
  ironAssign(projectRows().find(p=>/other/.test(p.name)).id, ironList[0].id);  // edit while unauthed
  const queued = relayOutbox.filter(o=>o.type==='prefs').length;
  window.__relayRx({type:'prefs',inactive:[],irons:JSON.parse(JSON.stringify(ironList.map(i=>({...i,keys:before}))))}); // stale echo
  const kept = ironList[0].keys.length;
  window.__sent.length=0; setRelayAuthed();
  const flushed = window.__sent.map(x=>{try{return JSON.parse(x);}catch{return null;}}).filter(Boolean)
                  .filter(x=>x.type==='prefs' && x.irons && x.irons[0].keys.length===2).length;
  // undo for the checks below, back on the authed socket
  ironAssign(projectRows().find(p=>/other/.test(p.name)).id, '');
  return { queued, kept, flushed };
});
check('an edit while the socket is UNAUTHED queues, survives the stale echo, and flushes on auth',
      unauthed.queued>=1 && unauthed.kept===2 && unauthed.flushed>=1, JSON.stringify(unauthed));
await page.evaluate(()=>{ document.querySelector('#ironrow .iprojchip.iadd').click(); });     // eyeball frame: the picker open
await page.screenshot({path:join(HERE,'ironprobe-add.png')});
await page.evaluate(()=>closeIronAdd());

// 5c. IRON SCOPE: opening a session from the iron page keeps you "inside" the
// iron — the tab strip shows only its sessions, the URL carries the iron, and
// climbing out lands back on the iron page (not the project's sessions rung).
await page.evaluate(id=>openIron(id), ironId);
await page.waitForTimeout(200);
const inline = await page.evaluate(()=>{
  const hud=[...document.querySelectorAll('#sessionbar .stab')].find(t=>/hud/.test(t.textContent));
  hud.click();                                     // switch sessions WITHOUT leaving the iron
  const tabs=[...document.querySelectorAll('#sessionbar .stab')].map(t=>({
    lbl:(t.querySelector('.lbl')||{}).textContent||'', active:t.classList.contains('active')}));
  const out={ view: currentView(), cid: currentCid, scope: ironScope,
              rowShown: !document.getElementById('ironrow').hidden, tabs };
  const mic=[...document.querySelectorAll('#sessionbar .stab')].find(t=>/mic/.test(t.textContent));
  mic.click();                                     // back for the checks below
  return out;
});
check('tapping another tab switches the terminal IN PLACE (view stays, row stays, highlight moves)',
      inline.view==='tty' && inline.cid==='ch1' && inline.scope===ironId && inline.rowShown
      && inline.tabs.find(t=>/hud/.test(t.lbl)).active && !inline.tabs.find(t=>/mic/.test(t.lbl)).active,
      JSON.stringify(inline));
const climb = await page.evaluate(()=>{
  document.getElementById('ironsBtn').click();     // 🔥 from a scoped session = the irons list
  return { view: currentView(), hash: location.hash };
});
check('the 🔥 header button from a scoped session goes to the irons list',
      climb.view==='irons' && climb.hash==='#/irons', JSON.stringify(climb));
const climb2 = await page.evaluate(id=>{
  openIron(id);                                    // re-enter the scoped session…
  goShallower();                                   // …and climb (Ctrl+Shift+↑ / swipe path)
  return { view: currentView() };
}, ironId);
check('goShallower from a scoped session climbs to the irons list',
      climb2.view==='irons', JSON.stringify(climb2));
await page.evaluate(id=>openIron(id), ironId);     // back in for the exit-scope check
const exitScope = await page.evaluate(()=>{ navTo('sessions');
  return { scope: ironScope, rowHidden: document.getElementById('ironrow').hidden }; });
check('navigating to a normal rung exits the scope (iron row gone)',
      exitScope.scope===null && exitScope.rowHidden, JSON.stringify(exitScope));
// entering a member session the NORMAL way (sessions rung / global strip) must
// NOT pull you into the iron — no row, no narrowed strip, no scope. Irons are
// entered ONLY through the 🔥 list or a #/i/ link. (The first cut auto-scoped
// here and yanked the full strip away mid-work — never again.)
const normalEntry = await page.evaluate(()=>{
  focusSession(allSessions().find(s=>s.cid==='ca1'));   // what a strip/rung tap does
  return { view: currentView(), scope: ironScope,
           rowHidden: document.getElementById('ironrow').hidden,
           tabs:[...document.querySelectorAll('#sessionbar .stab')].map(t=>t.querySelector('.lbl').textContent) };
});
check('opening a member session from the NORMAL path stays NORMAL (no row, full strip, no scope)',
      normalEntry.view==='tty' && normalEntry.scope===null && normalEntry.rowHidden
      && normalEntry.tabs.some(l=>/other/.test(l)),   // the outsider is still there = strip not scoped
      JSON.stringify(normalEntry));
await page.evaluate(()=>navTo('sessions'));            // back out for the deep-link check
// deep link straight into a scoped session (reload survival)
await page.evaluate(id=>{ location.hash='#/i/'+id+'/s/ca1/tty'; }, ironId);
await page.waitForTimeout(300);
const dl = await page.evaluate(()=>({ view: currentView(), scope: ironScope,
  rowShown: !document.getElementById('ironrow').hidden,
  tabs:[...document.querySelectorAll('#sessionbar .stab')].length }));
check('deep link #/i/<id>/s/<cid>/tty restores the scoped session view (row + full rail)',
      dl.view==='tty' && dl.scope===ironId && dl.rowShown && dl.tabs===2, JSON.stringify(dl));
await page.screenshot({path:join(HERE,'ironprobe-scoped.png')});   // eyeball frame: 🔥 header + iron tabs over the tty

// 6. deep link straight to the iron → its warmest session, scoped
await page.evaluate(id=>{ navTo('projects'); location.hash='#/i/'+id; }, ironId);
await page.waitForTimeout(300);
check('deep link #/i/<id> dives into the iron’s warmest session',
      await page.evaluate(id=>currentView()==='tty' && ironScope===id && currentCid==='ca1'
        && /voice/.test(document.getElementById('ironrow').innerText), ironId));
await page.screenshot({path:join(HERE,'ironprobe.png')});

// 6b. the dive-wait (2026-08-29): the iron card renders from `projects` frames
// but the dive needs `sessions` frames — clicking in that gap (channel still
// securing after wake / cold boot) must NOT open the add-project picker over an
// iron visibly full of projects. It holds the click open and dives the moment
// the frame lands; only a truly member-less iron falls through to the picker.
const gap = await page.evaluate(()=>{
  navTo('irons');
  const saved = {};                                // sessions frames "not arrived yet"
  for (const mid of Object.keys(perMachine)) {
    saved[mid] = { sl: perMachine[mid].sessionList, got: perMachine[mid].gotSessions };
    perMachine[mid].sessionList = []; perMachine[mid].gotSessions = false;
  }
  document.querySelector('#ironlist .ironcard').click();
  const mid_ = { view: currentView(), add: !document.getElementById('ironaddmodal').hidden,
                 waiting: /finding/.test(meta.textContent) };
  // the frame lands (the same call the WS handler makes) → the click completes
  for (const mid of Object.keys(saved)) {
    perMachine[mid].sessionList = saved[mid].sl; perMachine[mid].gotSessions = saved[mid].got;
  }
  maybeIronDive();
  return { mid_, after: { view: currentView(), cid: currentCid, scope: ironScope } };
});
check('clicking an iron before sessions frames land WAITS (no add-picker) …',
      gap.mid_.view==='irons' && !gap.mid_.add && gap.mid_.waiting, JSON.stringify(gap.mid_));
check('… and dives the moment the sessions frame arrives',
      gap.after.view==='tty' && gap.after.cid==='ca1', JSON.stringify(gap.after));
await page.evaluate(()=>navTo('irons'));

// 6c. taps survive the repaint storm (2026-08-29 "clicking an iron does
// NOTHING"): the list repaints on every frame from every machine, and the old
// innerHTML='' rebuild destroyed the card under the finger between pointerdown
// and click. Now (a) an unchanged card is left as the SAME DOM node across a
// repaint, and (b) the click is delegated to #ironlist, so even a click whose
// target card was rebuilt mid-tap still opens the iron.
const tap = await page.evaluate(()=>{
  navTo('irons');
  const list=document.getElementById('ironlist');
  const before=list.querySelector('.ironcard');
  renderIronList(); renderIronList();            // two frame repaints
  const sameNode = list.querySelector('.ironcard')===before;
  // a rebuilt card (innards swapped) must still open via the delegated listener
  const card=list.querySelector('.ironcard');
  card._ifp=null; renderIronList();              // force a re-fill of this card
  card.querySelector('.stitle').dispatchEvent(new MouseEvent('click',{bubbles:true}));
  return { sameNode, view: currentView(), cid: currentCid };
});
check('repaints keep the card as the SAME node (taps can\'t die on a removed one)', tap.sameNode);
check('the delegated click opens the iron even after a mid-tap re-fill',
      tap.view==='tty' && tap.cid==='ca1', JSON.stringify(tap));
await page.evaluate(()=>navTo('irons'));

// 7. 🔎 filter (the projects-rung deal) + ⠿ drag-to-reorder priority
await page.evaluate(()=>{
  ironCreate('metal','',[]);                       // second iron, via the real path
  // faithful echo so the dirty flag clears and later frames land
  window.__relayRx({type:'prefs',inactive:[],irons:JSON.parse(JSON.stringify(ironList))});
});
check('a NEW iron lands at the TOP of the priority order',
      await page.evaluate(()=>ironList[0].title==='metal'));
const filt = await page.evaluate(()=>{
  navTo('projects'); navTo('irons');               // fresh arrival
  const inp=document.getElementById('ironFilterInp');
  const focused=document.activeElement===inp;
  inp.value='voi'; inp.dispatchEvent(new Event('input',{bubbles:true}));
  const vis=()=>[...document.querySelectorAll('#ironlist .ironcard')]
              .filter(c=>c.style.display!=='none').map(c=>ironById(c.dataset.id).title);
  const narrowed=vis();
  renderIronList();                                // literally what a frame does
  const after={ focused: document.activeElement===inp, text: inp.value, vis: vis() };
  return { focused, narrowed, after };
});
check('landing on irons focuses the filter box', filt.focused, JSON.stringify(filt));
check('typing narrows the list', filt.narrowed.length===1 && filt.narrowed[0]==='voice', JSON.stringify(filt.narrowed));
check('the filter survives a repaint (focus + text + narrowing)',
      filt.after.focused && filt.after.text==='voi' && filt.after.vis.length===1, JSON.stringify(filt.after));
const enter = await page.evaluate(()=>{
  document.getElementById('ironFilterInp')
    .dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
  return { view: currentView(), scope: ironScope, cid: currentCid,
           cleared: document.getElementById('ironFilterInp').value==='' };
});
check('Enter on a lone match dives straight into that iron, filter cleared',
      enter.view==='tty' && enter.scope===ironId && enter.cid==='ca1' && enter.cleared, JSON.stringify(enter));
const drag = await page.evaluate(()=>{
  navTo('irons');
  const list=document.getElementById('ironlist');
  const cards=[...list.querySelectorAll('.ironcard')];
  const grab=cards[0].querySelector('.irondrag');
  if(!grab) return null;
  const bot=cards[cards.length-1].getBoundingClientRect();
  const domBefore=[...list.querySelectorAll('.ironcard')].map(c=>c.dataset.id).join();
  grab.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerId:7}));
  const stayed=currentView()==='irons';            // grabbing must NOT open the iron
  grab.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,pointerId:7,clientY:bot.bottom+5}));
  // mid-drag: real pick-up-and-carry — the card is LIFTED and riding the
  // pointer (fixed + transform), a spacer holds its slot, the sibling glides
  // via transform, and the DOM order hasn't budged (no snap-reorder mid-drag)
  const mid={ lifted: getComputedStyle(cards[0]).position==='fixed' && /translate/.test(cards[0].style.transform),
              spacer: !!list.querySelector('.ironph'),
              sibGlides: /translateY/.test(cards[1].style.transform) && cards[1].classList.contains('shifting'),
              domStill: [...list.querySelectorAll('.ironcard')].map(c=>c.dataset.id).join()===domBefore };
  window.__sent.length=0;
  grab.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:7}));
  const landed={ spacerGone: !list.querySelector('.ironph'),
                 clean: !cards[0].style.transform && cards[0].style.position==='' };
  const f=window.__sent.map(x=>{try{return JSON.parse(x);}catch{return null;}}).filter(Boolean)
          .find(x=>x.type==='prefs');
  return { stayed, mid, landed,
           dom:[...list.querySelectorAll('.ironcard')].map(c=>ironById(c.dataset.id).title),
           mem: ironList.map(i=>i.title),
           pushed: !!(f && f.irons && f.irons[0].title==='voice'), leaks: !!(f && 'inactive' in f) };
});
check('⠿ drag moves the card to the bottom and PERSISTS the order (irons-only push)',
      !!drag && drag.stayed && drag.dom[0]==='voice' && drag.dom[1]==='metal'
      && drag.mem[0]==='voice' && drag.pushed && !drag.leaks, JSON.stringify(drag));
check('mid-drag it CARRIES: card lifted on the pointer, spacer in its slot, sibling glides by transform, DOM order untouched',
      !!drag && drag.mid.lifted && drag.mid.spacer && drag.mid.sibGlides && drag.mid.domStill,
      JSON.stringify(drag && drag.mid));
check('drop lands clean: spacer gone, every drag style stripped',
      !!drag && drag.landed.spacerGone && drag.landed.clean, JSON.stringify(drag && drag.landed));
await page.close();

// ---- direct mode ------------------------------------------------------------
const dpage = await newPage(raw, 'https://direct.probe/?t=x');
await dpage.evaluate(()=>window.__relayRx({type:'projects',projects:[
  {pid:'p1',name:'alpha',repoUrl:'https://github.com/x/alpha',kind:'gh',status:'ready',sessionCount:1,busyCount:0,waitingCount:0,created:1,pinned:false,lastTouched:10,emoji:''}],boot:'b1'}));
await dpage.evaluate(()=>window.__relayRx({type:'sessions',sessions:[
  {cid:'c1',pid:'p1',title:'direct job',tab:'job',alive:true,busy:false,pinned:0,promptedAt:1,lastActive:1}],current:null}));
await dpage.evaluate(()=>window.__relayRx({type:'irons',irons:[
  {id:'i9',title:'direct iron',desc:'',tags:[],pids:['p1'],created:1},
  {id:'i8',title:'second iron',desc:'',tags:[],pids:[],created:2}]}));
await dpage.waitForTimeout(300);
const direct = await dpage.evaluate(()=>{
  const sent=()=>window.__sent.map(x=>{try{return JSON.parse(x);}catch{return null;}}).filter(Boolean);
  window.__sent.length=0;
  openIron('i9');                                        // dives into the member session + asks haiku for a desc
  const view = currentView(), scope = ironScope;
  const req = sent().find(x=>x.type==='ironDescribe');
  const placeholder = document.getElementById('ironrow').innerText.includes('describing…');
  window.__relayRx({type:'ironDesc', id:'i9', desc:'made by haiku'});
  const upd = sent().find(x=>x.type==='ironUpdate');
  const tabs=[...document.querySelectorAll('#sessionbar .stab')].map(t=>t.querySelector('.lbl').textContent);
  window.__sent.length=0;
  ironAssign('p1','');                                   // registry-backed op, not a prefs frame
  const op = sent()[0];
  return { view, scope, tabs, placeholder,
           req: req ? { id:req.id, hasCtx: /direct job/.test(req.context||'') } : null,
           upd: upd ? { id:upd.id, desc:upd.desc } : null,
           op: op && op.type, pid: op && op.pid };
});
check('direct mode: opening the iron dives into the member session (scoped strip)',
      direct.view==='tty' && direct.scope==='i9'
      && direct.tabs.length===1 && /job/.test(direct.tabs[0]), JSON.stringify(direct));
check('direct mode: the row requests a desc (session titles in context) + shows "describing…"',
      !!direct.req && direct.req.id==='i9' && direct.req.hasCtx && direct.placeholder, JSON.stringify(direct.req));
check('direct mode: haiku’s reply is stored via ironUpdate',
      !!direct.upd && direct.upd.id==='i9' && direct.upd.desc==='made by haiku', JSON.stringify(direct.upd));
check('direct mode: assignment is an ironAssign op (registry-backed)',
      direct.op==='ironAssign' && direct.pid==='p1', JSON.stringify(direct));
const dorder = await dpage.evaluate(()=>{
  window.__sent.length=0;
  ironReorder(['i8','i9']);                              // drag's drop path
  const op=window.__sent.map(x=>{try{return JSON.parse(x);}catch{return null;}}).filter(Boolean)
           .find(x=>x.type==='ironOrder');
  return { ids: op && op.ids, mem: ironList.map(i=>i.id) };
});
check('direct mode: reorder goes out as an ironOrder op, applied optimistically',
      !!dorder.ids && dorder.ids.join()==='i8,i9' && dorder.mem.join()==='i8,i9', JSON.stringify(dorder));
await dpage.close();

check('no page errors', errors.length===0, errors.join(' | '));
await browser.close();
console.log(failed?'FAIL':'PASS');
process.exit(failed?1:0);
