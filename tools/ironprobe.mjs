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
//   5. the create form (a live <input>) survives the repaint a frame triggers:
//      focus + un-mirrored text intact (the projects-rung rule)
//   6. deep link #/i/<id> lands on the iron page
// And in DIRECT mode: the harness `irons` frame renders, and assignment goes
// out as an ironAssign op (registry-backed), not a prefs frame.
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

// 2. create an iron (TITLE ONLY — desc is haiku's job, tags come later)
check('both forms are actually INVISIBLE until asked for (hidden beats display:flex)',
      await page.evaluate(()=>{
        const gone = el => getComputedStyle(el).display==='none';
        return gone(document.getElementById('ironform')) && gone(document.getElementById('ironedit'));
      }));
check('create form is title-only (no desc/tags inputs)',
      await page.evaluate(()=>document.querySelectorAll('#ironform input').length===1));
await page.evaluate(()=>{ window.__sent.length=0;
  document.getElementById('ironAddBtn').click();
  document.getElementById('ironFormTitle').value='voice';
  document.getElementById('ironFormSave').click();
});
await page.waitForTimeout(200);
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

// 5. repaint survival: un-mirrored text + focus in the create form
const form = await page.evaluate(()=>{
  document.getElementById('ironAddBtn').click();          // reopen the form
  const t=document.getElementById('ironFormTitle');
  t.focus(); t.value='half-typ';
  renderIronList();                                       // literally what a frame does
  return { focused: document.activeElement===t, text: t.value };
});
check('create form survives a repaint (focus + un-mirrored text)', form.focused && form.text==='half-typ', JSON.stringify(form));
await page.evaluate(()=>{ document.getElementById('ironFormCancel').click(); });

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

// 4. the iron page: header + cross-machine tabs, pinned at the end
await page.evaluate(id=>openIron(id), ironId);
await page.waitForTimeout(300);
const detail = await page.evaluate(()=>{
  const body=document.getElementById('ironbody');
  const tabs=[...document.querySelectorAll('#irontabs .stab')];
  return { hash:location.hash, text:body.innerText.slice(0,400),
           tabs:tabs.map(t=>({lbl:t.querySelector('.lbl').textContent, parked:t.classList.contains('parked')})) };
});
check('deep hash #/i/<id> is written', detail.hash==='#/i/'+ironId, detail.hash);
check('title + desc + tags render', /voice/.test(detail.text) && /all the voice work/.test(detail.text) && /speech/.test(detail.text));
check('sessions from BOTH machines of the member project show',
      detail.tabs.length===2 && detail.tabs.some(t=>/mic/.test(t.lbl)) && detail.tabs.some(t=>/hud/.test(t.lbl)),
      JSON.stringify(detail.tabs));
check('📌 pinned member sits at the END, marked',
      detail.tabs.length===2 && detail.tabs[1].parked && /^📌 /.test(detail.tabs[1].lbl) && !detail.tabs[0].parked,
      JSON.stringify(detail.tabs));
check('the unrelated project’s session stays out', !detail.tabs.some(t=>/other/.test(t.lbl)));
check('global tab strip is hidden on the iron page (it has its own row)',
      await page.evaluate(()=>document.getElementById('sessionbar').hidden));
// the AI desc: a reply from ironDescribe is stored (prefs push) + rendered
const descFlow = await page.evaluate(()=>{
  window.__sent.length=0;
  onIronDesc({ id: ironList[0].id, desc: 'haiku wrote this' });
  const f=window.__sent.map(x=>{try{return JSON.parse(x);}catch{return null;}}).filter(Boolean)
          .find(x=>x.type==='prefs');
  return { desc: ironList[0].desc,
           pushed: !!(f && f.irons && f.irons[0].desc==='haiku wrote this' && !('inactive' in f)),
           shown: document.getElementById('ironbody').innerText.includes('haiku wrote this') };
});
check('haiku’s desc is stored (irons-only push) + rendered',
      descFlow.desc==='haiku wrote this' && descFlow.pushed && descFlow.shown, JSON.stringify(descFlow));

// 5b. the ＋ add-project picker on the iron page: a toggle checklist — the row
// you tap turns ✓ in place (never vanishes/reshuffles), and the write survives
// an unauthed socket (the 2026-08-26 production loss: mutate locally, frame
// silently dropped, next prefs frame reverts).
const addFlow = await page.evaluate(()=>{
  const modal=document.getElementById('ironaddmodal');
  const idle=getComputedStyle(modal).display==='none';       // invisible until asked
  const chip=document.querySelector('#ironbody .iprojchip.iadd');
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
await page.evaluate(()=>{ document.querySelector('#ironbody .iprojchip.iadd').click(); });     // eyeball frame: the picker open
await page.screenshot({path:join(HERE,'ironprobe-add.png')});
await page.evaluate(()=>closeIronAdd());

// 6. deep link straight to the iron
await page.evaluate(id=>{ navTo('projects'); location.hash='#/i/'+id; }, ironId);
await page.waitForTimeout(300);
check('deep link #/i/<id> lands on the iron page',
      await page.evaluate(()=>currentView()==='iron' && /voice/.test(document.getElementById('ironbody').innerText)));
await page.screenshot({path:join(HERE,'ironprobe.png')});
await page.close();

// ---- direct mode ------------------------------------------------------------
const dpage = await newPage(raw, 'https://direct.probe/?t=x');
await dpage.evaluate(()=>window.__relayRx({type:'projects',projects:[
  {pid:'p1',name:'alpha',repoUrl:'https://github.com/x/alpha',kind:'gh',status:'ready',sessionCount:1,busyCount:0,waitingCount:0,created:1,pinned:false,lastTouched:10,emoji:''}],boot:'b1'}));
await dpage.evaluate(()=>window.__relayRx({type:'sessions',sessions:[
  {cid:'c1',pid:'p1',title:'direct job',tab:'job',alive:true,busy:false,pinned:0,promptedAt:1,lastActive:1}],current:null}));
await dpage.evaluate(()=>window.__relayRx({type:'irons',irons:[
  {id:'i9',title:'direct iron',desc:'',tags:[],pids:['p1'],created:1}]}));
await dpage.waitForTimeout(300);
const direct = await dpage.evaluate(()=>{
  const sent=()=>window.__sent.map(x=>{try{return JSON.parse(x);}catch{return null;}}).filter(Boolean);
  window.__sent.length=0;
  currentIronId='i9'; navTo('iron');                     // opening the page asks haiku for a desc
  const req = sent().find(x=>x.type==='ironDescribe');
  const placeholder = document.getElementById('ironbody').innerText.includes('describing…');
  window.__relayRx({type:'ironDesc', id:'i9', desc:'made by haiku'});
  const upd = sent().find(x=>x.type==='ironUpdate');
  const tabs=[...document.querySelectorAll('#irontabs .stab')].map(t=>t.querySelector('.lbl').textContent);
  window.__sent.length=0;
  ironAssign('p1','');                                   // registry-backed op, not a prefs frame
  const op = sent()[0];
  return { tabs, placeholder,
           req: req ? { id:req.id, hasCtx: /direct job/.test(req.context||'') } : null,
           upd: upd ? { id:upd.id, desc:upd.desc } : null,
           op: op && op.type, pid: op && op.pid };
});
check('direct mode: harness `irons` frame renders the member session',
      direct.tabs.length===1 && /job/.test(direct.tabs[0]), JSON.stringify(direct.tabs));
check('direct mode: opening the page requests a desc (session titles in context) + shows "describing…"',
      !!direct.req && direct.req.id==='i9' && direct.req.hasCtx && direct.placeholder, JSON.stringify(direct.req));
check('direct mode: haiku’s reply is stored via ironUpdate',
      !!direct.upd && direct.upd.id==='i9' && direct.upd.desc==='made by haiku', JSON.stringify(direct.upd));
check('direct mode: assignment is an ironAssign op (registry-backed)',
      direct.op==='ironAssign' && direct.pid==='p1', JSON.stringify(direct));
await dpage.close();

check('no page errors', errors.length===0, errors.join(' | '));
await browser.close();
console.log(failed?'FAIL':'PASS');
process.exit(failed?1:0);
