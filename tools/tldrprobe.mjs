// tldrprobe — the 🟦 live TLDR block (2026-09-04): the blue plain-English
// summary floating over the terminal while claude writes. Contract pinned:
//   1. off by default: a tldr frame with the toggle off shows nothing;
//   2. tapping 🟦 (real touch) turns it on, persists, and sends {type:'tldr',on:true};
//   2b. the row is a FIXED-height footer slot whenever the mode is on — its height
//       must not change between passes (a footer resize = PTY resize + replay storm);
//   3. a tldr frame for the VIEWED session paints the block (live = "updating…");
//   4. a later frame REPLACES the text (rolling summary), final drops "updating";
//   5. a frame for another session is ignored;
//   6. an empty-text frame (new prompt) blanks it; so does UserPromptSubmit;
//   7. typing in the composer KEEPS it; sending clears it (you're done reading);
//   8. it sits in the footer directly above the session-name row (#descrow), as
//      light-blue text on the normal background — not a box over the terminal;
//   9. subscribe re-asserts the preference (tldr frame right after subscribe).
// Fleet mode + stubbed relay WebSocket (tapprobe pattern): no server, no session.
//   cd tools && node tldrprobe.mjs
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
await page.evaluate(()=>{ window.__relayRx({type:'prefs',inactive:[],irons:[]});
  window.__relayRx({type:'machines',machines:[{id:'clawd-atg',host:'atg',kind:'machine',online:true,lastSeen:0,stats:{projects:1,sessions:1,active:0}}]});
  handleMachineJson('clawd-atg',{type:'projects',projects:[
    {pid:'p1',name:'alpha',repoUrl:'https://github.com/clawdbotatg/alpha',kind:'gh',status:'ready',sessionCount:1,busyCount:0,waitingCount:0,created:1,pinned:false,lastTouched:100,emoji:''}]});
  handleMachineJson('clawd-atg',{type:'sessions',sessions:[
    {cid:'cid-probe-1',pid:'p1',title:'probe tldr',desc:'',promptCount:2,alive:true,busy:true,autopilot:false,pilotStatus:'',pilotRounds:0,lastActive:Date.now()/1000,promptedAt:Date.now()/1000}]});
});
await page.waitForTimeout(300);
// land on alpha's sessions rung, where the composer (and the 🟦 toggle) lives
await page.evaluate(()=>{ location.hash = '#/p/' + encodeURIComponent(projectRows().find(p=>p.name==='alpha').id); });
await page.waitForTimeout(400);
const CID='cid-probe-1';
const TLDR_HOLD_GAP_PROBE = 4;

// stub hsend so the toggle/subscribe frames are observable; pretend we're viewing the session
// open the session's tty view for real (the row that holds 🤖 + 🟦 only lays out there)
await page.evaluate((CID)=>{ window.__frames=[]; hsend=(f)=>{window.__frames.push(f);return true;};
  location.hash = '#/m/clawd-atg/p/' + encodeURIComponent(projectRows().find(p=>p.name==='alpha').id) + '/s/' + CID + '/tty'; }, CID);
await page.waitForTimeout(600);
await page.evaluate((CID)=>{ currentCid=CID; renderPilotUI(); }, CID);
check('landed in the tty view', await page.evaluate(()=>currentView()==='tty'));
// real fonts give fractional row heights (18.67px…); force one so index×cell drift would show
await page.evaluate(()=>{ term.options.lineHeight = 1.1; fit.fit(); });
await page.waitForTimeout(200);
const cellFrac = await page.evaluate(()=>term.element.querySelector('.xterm-rows').children[0].getBoundingClientRect().height);
check('probe runs on a fractional row height', Math.abs(cellFrac - Math.round(cellFrac)) > 0.05, String(cellFrac));

// 1. off by default
await page.evaluate((CID)=>handleJson({type:'tldr',cid:CID,text:'should not show',final:false}), CID);
check('off by default: frame shows nothing', await page.evaluate(()=>tldrEl.hidden && !tldrOn));
const gone = () => page.evaluate(()=>tldrTextEl.textContent==='' && tldrEl.classList.contains('empty'));

// 2. tap 🟦 (real touch)
const cdp = await page.context().newCDPSession(page);
const xy = await page.evaluate(()=>{const r=tldrBtn.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,visible:r.width>0&&r.height>0};});
check('🟦 toggle visible, left of the 🤖 checkbox', xy.visible && await page.evaluate(()=>tldrBtn.nextElementSibling===pilotChk && !pilotChk.hidden), JSON.stringify(xy));
await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:xy.x,y:xy.y}]});
await page.waitForTimeout(80);
await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
await page.waitForTimeout(200);
const on = await page.evaluate((CID)=>({on:tldrOn, cls:tldrBtn.classList.contains('on'), ls:localStorage.getItem('cc_tldr'),
  frame:window.__frames.find(f=>f.type==='tldr'&&f.cid===CID&&f.on===true)}), CID);
check('tap turns it on + persists + sends the verb', on.on && on.cls && on.ls==='1' && !!on.frame, JSON.stringify(on));
// paint claude-like chrome first: a thinking line, blank, rule, prompt, rule, status, 2 blank rows under
await page.evaluate(()=>new Promise(res=>term.write('\x1b[2J\x1b[H* Churned for 1m\r\n\r\n────────\r\n❯ \r\n────────\r\n  bypass permissions on\r\n\r\n\r\n', res)));
await page.waitForTimeout(150);
const h0 = await page.evaluate(()=>{ positionTldr(); return {hidden:tldrEl.hidden, h:tldrEl.getBoundingClientRect().height, footer:document.querySelector('footer').getBoundingClientRect().height, term:document.getElementById('term').getBoundingClientRect().height}; });
check('mode on but no summary yet: no overlay', h0.hidden, JSON.stringify(h0));
// …and no chrome either: the hold slides claude's bottom rows past #term's clip edge (short regime)
const hold0 = await page.evaluate(()=>{ applyTldrHold(); const rowsEl=term.element.querySelector('.xterm-rows');
  let last=-1; for (let i=0;i<rowsEl.children.length;i++) if (rowsEl.children[i].textContent.trim()) last=i;
  const tb=document.getElementById('term').getBoundingClientRect().bottom;
  return {hold:tldrHold(), thinkingBottom:rowsEl.children[0].getBoundingClientRect().bottom, ruleTop:rowsEl.children[last-3].getBoundingClientRect().top, statusTop:rowsEl.children[last].getBoundingClientRect().top, termBottom:tb}; });
check('no summary: thinking line visible, input box + status pushed below the fold', hold0.hold===5 && hold0.thinkingBottom<=hold0.termBottom && hold0.ruleTop>=hold0.termBottom-1 && hold0.statusTop>=hold0.termBottom, JSON.stringify(hold0));

// 3. a frame paints the overlay, live
await page.evaluate((CID)=>handleJson({type:'tldr',cid:CID,text:'Fixed the bug. Tests pass.',final:false}), CID);
await page.waitForTimeout(100);
let st = await page.evaluate(()=>{ const rowsEl=term.element.querySelector('.xterm-rows');
  let last=-1; for (let i=0;i<rowsEl.children.length;i++) if (rowsEl.children[i].textContent.trim()) last=i;
  const r=tldrEl.getBoundingClientRect(); const left=document.getElementById('left').getBoundingClientRect();
  return {hidden:tldrEl.hidden, text:tldrText.textContent, live:tldrEl.classList.contains('live'),
  pos:getComputedStyle(tldrEl).position, parent:tldrEl.parentElement.id,
  color:getComputedStyle(tldrEl).color, bg:getComputedStyle(tldrEl).backgroundColor,
  top:r.top, want:rowsEl.children[last-TTY_COVER_ROWS+1].getBoundingClientRect().top,
  thinking:rowsEl.children[0].getBoundingClientRect().bottom, bottom:r.bottom, leftBottom:left.bottom}; });
check('frame paints the overlay, marked updating', !st.hidden && st.text==='Fixed the bug. Tests pass.' && st.live, JSON.stringify(st));
check('overlay in #left, flush under the thinking line, down to the footer', st.pos==='absolute' && st.parent==='left' && Math.abs(st.top-st.want)<=0.6 && Math.abs(st.top-st.thinking)<=0.6 && Math.abs(st.bottom-st.leftBottom)<=1, JSON.stringify(st));
check('light-blue text on black', st.color==='rgb(74, 158, 255)' && st.bg==='rgb(0, 0, 0)', JSON.stringify(st));
const h1 = await page.evaluate(()=>({h:tldrEl.getBoundingClientRect().height, footer:document.querySelector('footer').getBoundingClientRect().height, term:document.getElementById('term').getBoundingClientRect().height}));
check('painting text changed neither the footer nor #term height', h1.footer===h0.footer && h1.term===h0.term, JSON.stringify({h0,h1}));
await page.evaluate((CID)=>handleJson({type:'tldr',cid:CID,text:('Long line of summary text. ').repeat(40),final:false}), CID);
await page.waitForTimeout(100);
const h2 = await page.evaluate(()=>({h:tldrEl.getBoundingClientRect().height, footer:document.querySelector('footer').getBoundingClientRect().height, term:document.getElementById('term').getBoundingClientRect().height, scrolls:tldrEl.scrollHeight>tldrEl.clientHeight}));
check('a long summary scrolls inside; footer and #term unchanged', h2.footer===h0.footer && h2.term===h0.term && h2.scrolls, JSON.stringify({h0,h2}));
// scrollback regime: fill the buffer, then with no summary the viewport sits 5 rows up
await page.evaluate((CID)=>handleJson({type:'tldr',cid:CID,text:'',final:false}), CID);
await page.evaluate(()=>new Promise(res=>{ let t=''; for (let i=0;i<80;i++) t+='line '+i+'\r\n'; t+='* Thinking…\r\n\r\n────────\r\n❯ \r\n────────\r\n  bypass permissions on\r\n'; term.write(t, res); }));
await page.waitForTimeout(150);
const sb = await page.evaluate(()=>{ ttyBottom(); const b=term.buffer.active; return {baseY:b.baseY, dist:b.baseY-b.viewportY, atBottom:ttyAtBottom(), hold:tldrHold(), trailing:tldrTrailing()}; });
check('scrollback, no summary: viewport held 5 rows + the blank cursor line up, still counts as at-bottom', sb.baseY>0 && sb.trailing===1 && sb.dist===5+sb.trailing && sb.atBottom && sb.hold===5, JSON.stringify(sb));
await page.waitForTimeout(100);
const gap = await page.evaluate(()=>{ const rowsEl=term.element.querySelector('.xterm-rows'); let lastRow=-1; for (let i=0;i<rowsEl.children.length;i++) if (rowsEl.children[i].textContent.trim()) lastRow=i;
  const r=rowsEl.children[lastRow].getBoundingClientRect(), t=document.getElementById('term').getBoundingClientRect(); return {text:rowsEl.children[lastRow].textContent.trim().slice(0,20), gap:+(t.bottom-r.bottom).toFixed(1)}; });
check('no summary: the thinking line is last, with a few px of air under it', gap.text.startsWith('* Thinking') && gap.gap>=TLDR_HOLD_GAP_PROBE && gap.gap<TLDR_HOLD_GAP_PROBE+20, JSON.stringify(gap));
await page.evaluate((CID)=>handleJson({type:'tldr',cid:CID,text:'summary back',final:false}), CID);
await page.waitForTimeout(120);
const sb2 = await page.evaluate(()=>{ const b=term.buffer.active; const rowsEl=term.element.querySelector('.xterm-rows');
  const r=tldrEl.getBoundingClientRect(), left=document.getElementById('left').getBoundingClientRect();
  // the line above the chrome ("* Thinking…") must be the last visible terminal row, right above the box
  let think=-1; for (let i=0;i<rowsEl.children.length;i++) if (rowsEl.children[i].textContent.includes('Thinking')) think=i;
  const tr = think>=0 ? rowsEl.children[think].getBoundingClientRect() : null;
  return {dist:b.baseY-b.viewportY, hidden:tldrEl.hidden, hold:tldrHold(), trailing:tldrTrailing(), boxTop:r.top, boxBottom:r.bottom, leftBottom:left.bottom, thinkBottom: tr&&tr.bottom, thinkTop: tr&&tr.top, termTop:document.getElementById('term').getBoundingClientRect().top}; });
check('one-line summary in scrollback: viewport held (5 − rows needed) up, box sits on the footer right under the thinking line',
  sb2.dist===sb2.hold+sb2.trailing && sb2.hold>0 && sb2.hold<5 && !sb2.hidden && Math.abs(sb2.boxBottom-sb2.leftBottom)<=1 && sb2.thinkBottom!=null && Math.abs(sb2.boxTop-sb2.thinkBottom)<=0.6 && sb2.thinkTop>=sb2.termTop-0.5, JSON.stringify(sb2));
const fit1 = await page.evaluate(()=>{ const rowsEl=term.element.querySelector('.xterm-rows'); const cell=rowsEl.children[0].offsetHeight;
  const r=tldrEl.getBoundingClientRect(), t=tldrInner.getBoundingClientRect();
  const screen=term.element.querySelector('.xterm-screen'); let last=-1; for (let i=0;i<rowsEl.children.length;i++) if (rowsEl.children[i].textContent.trim()) last=i;
  return {cell, boxH:r.height, textH:t.height, slackBelow:r.bottom-t.bottom, hold:tldrHold(), need:tldrRowsNeeded(), transform:screen.style.transform, last, baseY:term.buffer.active.baseY, screenTop:screen.getBoundingClientRect().top, boxTop:r.top, holdLast:tldrHoldLast}; });
const needWant = Math.ceil((fit1.textH + 5) / fit1.cell);
check('one-line summary: box only as tall as its text, text at its bottom, the rest of the chrome held out of sight',
  fit1.need===needWant && fit1.hold===5-needWant && fit1.boxH <= (needWant+1)*fit1.cell && fit1.slackBelow <= fit1.cell/2 + 4, JSON.stringify(fit1));
await page.evaluate(()=>new Promise(res=>term.write('\x1b[2J\x1b[H* Churned for 1m\r\n\r\n────────\r\n❯ \r\n────────\r\n  bypass permissions on\r\n\r\n\r\n', res)));
await page.waitForTimeout(150);
await page.evaluate((CID)=>handleJson({type:'tldr',cid:CID,text:'Fixed the bug. Tests pass.',final:false}), CID);

// 4. a later frame replaces the text; final drops "updating"
await page.evaluate((CID)=>handleJson({type:'tldr',cid:CID,text:'Fixed the bug. Tests pass. Pushed.',final:true}), CID);
st = await page.evaluate(()=>({text:tldrText.textContent, live:tldrEl.classList.contains('live')}));
check('rolling: later frame replaces, final = settled', st.text==='Fixed the bug. Tests pass. Pushed.' && !st.live, JSON.stringify(st));

// 5. another session's frame is ignored
await page.evaluate(()=>handleJson({type:'tldr',cid:'cid-other',text:'WRONG SESSION',final:true}));
check('other session ignored', await page.evaluate(()=>tldrText.textContent==='Fixed the bug. Tests pass. Pushed.'));

// 6. empty text = new turn → blank; UserPromptSubmit too
await page.evaluate((CID)=>handleJson({type:'tldr',cid:CID,text:'',final:false}), CID);
check('empty frame blanks it and drops the overlay', (await gone()) && await page.evaluate(()=>tldrEl.hidden));
await page.evaluate((CID)=>{handleJson({type:'tldr',cid:CID,text:'again',final:false}); handleJson({type:'hook',cid:CID,event:'UserPromptSubmit',busy:true,waiting:false,tool:null,data:{prompt:'x'}});}, CID);
check('UserPromptSubmit blanks it', await gone());

// 7. typing keeps it; sending clears it
await page.evaluate((CID)=>handleJson({type:'tldr',cid:CID,text:'read me',final:true}), CID);
check('shown before typing', await page.evaluate(()=>tldrTextEl.textContent==='read me'));
await page.evaluate(()=>{ box.focus(); });
await page.keyboard.type('ok');
check('typing keeps it on screen', await page.evaluate(()=>tldrTextEl.textContent==='read me' && box.value==='ok'));
await page.evaluate((CID)=>deliverSend(CID, 'ok', null, 'typed'), CID);
check('sending clears it', await gone());
await page.evaluate(()=>{ box.value=''; });

// 7b. switching to another session blanks it — the summary must not follow you
await page.evaluate((CID)=>handleJson({type:'tldr',cid:CID,text:'stay here',final:true}), CID);
check('shown before the switch', await page.evaluate(()=>tldrTextEl.textContent==='stay here'));
await page.evaluate(()=>{ subscribe('cid-other'); });
check('switching sessions blanks the summary', (await gone()) && await page.evaluate(()=>currentCid==='cid-other'));
await page.evaluate(()=>handleJson({type:'tldr',cid:'cid-probe-1',text:'late frame for the OLD session',final:true}));
check("the old session's late frame doesn't paint", await gone());
await page.evaluate((CID)=>{ subscribe(CID); }, CID);

// 8. subscribe re-asserts the preference
await page.evaluate((CID)=>{ window.__frames.length=0; subscribe(CID); }, CID);
const sub = await page.evaluate(()=>window.__frames.map(f=>f.type));
check('subscribe is followed by the tldr verb', sub[0]==='subscribe' && sub[1]==='tldr', JSON.stringify(sub));

// (subscribe() reset the grid; repaint claude-like chrome so the overlay has rows to sit on)
await page.evaluate(()=>new Promise(res=>term.write('\x1b[2J\x1b[H* Churned for 1m\r\n\r\n────────\r\n❯ \r\n────────\r\n  bypass permissions on\r\n\r\n\r\n', res)));
await page.waitForTimeout(150);
await page.evaluate((CID)=>{ handleJson({type:'tldr',cid:CID,text:'back with text',final:false}); }, CID);
check('overlay back once there is text again', await page.evaluate(()=>!tldrEl.hidden));

// 8b. the ✕ marks it read (blanks + sends the mark); tapping the TEXT leaves it (selectable, copyable)
await page.evaluate((CID)=>{ handleJson({type:'tldr',cid:CID,text:'read me now',final:false}); window.__frames.length=0; }, CID);
const txy = await page.evaluate(()=>{const r=tldrText.getBoundingClientRect();return {x:r.x+20,y:r.y+r.height/2};});
await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:txy.x,y:txy.y}]});
await page.waitForTimeout(80);
await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
await page.waitForTimeout(200);
check('tap on the text does NOT clear it (selectable)', await page.evaluate(()=>tldrText.textContent==='read me now' && getComputedStyle(tldrEl).userSelect==='text' && window.__frames.length===0));
const xxy = await page.evaluate(()=>{const r=tldrX.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,visible:r.width>0};});
check('✕ visible in the box', xxy.visible);
await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:xxy.x,y:xxy.y}]});
await page.waitForTimeout(80);
await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
await page.waitForTimeout(200);
check('✕ blanks it + sends mark', (await gone()) && await page.evaluate((CID)=>window.__frames.some(f=>f.type==='tldr'&&f.cid===CID&&f.mark===true), CID));

// 8b2. the DONE layout has two blank rows above the rules: the box must still start right under the done line
await page.evaluate(()=>new Promise(res=>{ let t=''; for (let i=0;i<80;i++) t+='line '+i+'\r\n'; t+='* Crunched for 20s · done\r\n\r\n\r\n────────\r\n❯ \r\n────────\r\n  bypass permissions on\r\n'; term.write(t, res); }));
await page.waitForTimeout(150);
await page.evaluate((CID)=>handleJson({type:'tldr',cid:CID,text:'done-state summary',final:true}), CID);
await page.waitForTimeout(150);
const dn = await page.evaluate(()=>{ const rowsEl=term.element.querySelector('.xterm-rows'); const r=tldrEl.getBoundingClientRect();
  let done=-1; for (let i=0;i<rowsEl.children.length;i++) if (rowsEl.children[i].textContent.includes('Crunched')) done=i;
  const dr = done>=0 ? rowsEl.children[done].getBoundingClientRect() : null;
  return {chromeRows:tldrChromeRows(), hold:tldrHold(), boxTop:r.top, doneBottom:dr&&dr.bottom, gap: dr ? +(r.top-dr.bottom).toFixed(1) : null}; });
check('done layout (two blank rows): chrome measured as 6, box starts right under the done line', dn.chromeRows===6 && dn.doneBottom!=null && Math.abs(dn.gap)<=0.6, JSON.stringify(dn));

// 8c. the overlay lifts while the session waits on you (prompts render right there)
await page.evaluate((CID)=>handleJson({type:'tldr',cid:CID,text:'text again',final:false}), CID);
check('overlay up before the wait', await page.evaluate(()=>!tldrEl.hidden));
const sessFrame = (waiting) => ({type:'sessions',sessions:[
  {cid:'cid-probe-1',pid:'p1',title:'probe tldr',desc:'',promptCount:2,alive:true,busy:true,waiting,autopilot:false,pilotStatus:'',pilotRounds:0,lastActive:Date.now()/1000,promptedAt:Date.now()/1000}]});
await page.evaluate((f)=>{ handleMachineJson('clawd-atg', f); renderPilotUI(); }, sessFrame(true));
check('overlay lifts while the session waits on you', await page.evaluate(()=>tldrEl.hidden));
await page.evaluate((f)=>{ handleMachineJson('clawd-atg', f); renderPilotUI(); }, sessFrame(false));
check('…and returns', await page.evaluate(()=>!tldrEl.hidden));

// 9. off again hides + sends off
await page.evaluate((CID)=>{handleJson({type:'tldr',cid:CID,text:'bye',final:true}); window.__frames.length=0; setTldr(false);}, CID);
check('off drops the overlay + sends the verb', await page.evaluate(()=>tldrEl.hidden && !tldrOn && window.__frames.some(f=>f.type==='tldr'&&f.on===false)));

check('no page errors', errors.length===0, errors.join(' | '));

// 10. COLD START with the mode persisted on: the page must still boot and dial the
// relay. The overlay code runs during boot (clearTldr(), xterm renders); walking into
// currentView() there hit a not-yet-declared const and aborted the script before
// connect() — every h.atg.link load sat at "connecting…" (2026-09-04).
const errors2=[];
const page2 = await browser.newPage({ ...iphone, viewport:{width:390,height:844} });
await page2.addInitScript(() => {
  window.__sockets=0;
  class FakeWS{constructor(u){this.url=u;this.readyState=0;this.binaryType='arraybuffer';window.__sockets++;
    setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen({});},0);}
   send(){} close(){this.readyState=3;this.onclose&&this.onclose({});}}
  FakeWS.prototype.addEventListener=function(){}; window.WebSocket=FakeWS;
  try{localStorage.clear(); localStorage.setItem('cc_tldr','1');}catch{}
});
page2.on('pageerror',e=>errors2.push(String(e)));
await page2.route('https://fleet.probe/', r=>r.fulfill({status:200,contentType:'text/html; charset=utf-8',body:fleetHtml}));
await page2.goto('https://fleet.probe/#/p/self/s/abc/tty',{waitUntil:'domcontentloaded'});
await page2.waitForTimeout(800);
const cold = await page2.evaluate(()=>({sockets:window.__sockets, meta:document.getElementById('meta').textContent, on:tldrOn, hidden:tldrEl.hidden}));
check('cold start with tldr persisted on: no page errors', errors2.length===0, errors2.join(' | '));
check('…the relay socket is dialed', cold.sockets>=1 && cold.meta!=='connecting…', JSON.stringify(cold));
check('…mode is on, overlay down until there is text', cold.on===true && cold.hidden===true, JSON.stringify(cold));
await page2.close();
await browser.close();
if (failed) { console.log('tldrprobe: FAIL'); process.exit(1); } else console.log('tldrprobe: all green');
