// sttprobe — composer dictation (🎤 push-to-talk) on an emulated iPhone, with
// SpeechRecognition stubbed. Guards the 2026-09-01 clobber bug: recognition
// results landing late (they trail rec.stop()) or from a session stranded in
// the on state used to blindly `box.value = …`, replacing text the user had
// typed since — "text came rushing in and replaced what I was typing".
//
// The contract pinned here:
//   1. mic hold → results write into the box; dictated text is saved as a draft;
//   2. a result trailing the release still lands — IF the box is untouched;
//   3. once the user types with the mic OFF, no late result may replace the box;
//   4. typing with the mic ON does not stop it (2026-09-15): the edit becomes
//      the new base, dictation carries on after it, the caret stays put, and
//      an interim being shown at the edit isn't doubled when its final lands;
//   4b. a composer context switch (leaving the rung) kills dictation;
//   5. #micBtn carries touch-action:none so a pan can't pointercancel the hold.
//
// Second act (desktop page): SPACE-HOLD push-to-talk in the composer —
//   6. a quick space tap types a normal space, never records;
//   7. holding space past the threshold rolls back the space(s) the key typed
//      (auto-repeat included) and starts dictation; release stops it;
//   8. auto-repeat spaces are eaten while dictating;
//   9. any other key during the wait cancels the pending hold (space kept).
//
// Third act (touch page again): 🎯 the DEEPGRAM engine behind the same gesture —
//  10. with creds in hand a hold opens the page's own socket to Deepgram
//      (subprotocol = the creds, URL = nova-3 + linear16 + every keyterm: the ⚙️
//      word list, the harness's names, each project name) — the relay socket
//      is not touched;
//  11. Deepgram Results land through the SAME guard: interim trails, finals
//      append, typing still wins and a late result can't clobber;
//  12. release sends CloseStream (finals may trail) and stops the mic tracks;
//  13. no creds (or another box's creds) → Web Speech, decided at the press.
//
// Fleet mode + stubbed relay WebSocket (tapprobe pattern): no real server, no
// real session, no mic. Real touch gestures via CDP, not element.click().
//   cd tools && node sttprobe.mjs
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
const initStub = () => {
  window.__sent=[]; const sockets=[];
  window.__dgs=[];
  class FakeWS{constructor(u,protos){this.url=u;this.protocols=protos||null;this.readyState=0;this.binaryType='arraybuffer';this.sent=[];
    (u.includes('deepgram')?window.__dgs:sockets).push(this);
    setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen({});},0);}
   send(d){window.__sent.push(d);this.sent.push(d);} close(){this.readyState=3;this.onclose&&this.onclose({});}}
  FakeWS.prototype.addEventListener=function(){}; window.WebSocket=FakeWS;
  window.__relayRx=(o)=>{const s=sockets[sockets.length-1]; if(s&&s.onmessage) s.onmessage({data:JSON.stringify(o)});};
  // a mic + audio graph that never ticks: the Deepgram act checks the socket, not audio
  window.__tracks=[];
  navigator.mediaDevices=Object.assign(navigator.mediaDevices||{}, {getUserMedia: async()=>{ const t={stopped:false,stop(){this.stopped=true;}}; window.__tracks.push(t); return {getTracks:()=>[t]}; }});
  window.AudioContext=class{constructor(){this.sampleRate=48000;this.state='running';this.destination={};}
    resume(){return Promise.resolve();} createMediaStreamSource(){return {connect(){},disconnect(){}};}
    createScriptProcessor(){return {connect(){},disconnect(){},onaudioprocess:null};}};
  window.__dgResult=(text,isFinal)=>{const s=window.__dgs[window.__dgs.length-1]; s.onmessage({data:JSON.stringify({type:'Results',is_final:!!isFinal,channel:{alternatives:[{transcript:text}]}})});};
  try{localStorage.clear();}catch{}
  for (const m of ['clawd-atg'])
    try{localStorage.setItem('cc_e2e_rs_'+m, JSON.stringify({id:'p-'+m,master:'AAAA',exp:Date.now()+3600e3}));}catch{}
  // stub SpeechRecognition BEFORE the app script sniffs for it
  window.__srs = [];
  window.SpeechRecognition = class {
    constructor(){ this.startedCount=0; this.live=false; window.__srs.push(this); window.__sr=this; }
    start(){ this.startedCount++; this.live=true; }
    stop(){ this.live=false; const f=this.onend; if (f) setTimeout(()=>f(),0); }
    abort(){ this.live=false; }
  };
  // emit a recognition result the way Chrome shapes it: only NEW finals per event
  window.__emit=(finals,interim)=>{
    const results=(finals||[]).map(t=>Object.assign([{transcript:t}],{isFinal:true}));
    if (interim!=null) results.push(Object.assign([{transcript:interim}],{isFinal:false}));
    window.__sr.onresult({resultIndex:0,results});
  };
};
await page.addInitScript(initStub);
page.on('pageerror',e=>errors.push(String(e)));
const bootPage = async (pg) => {
  await pg.route('https://fleet.probe/', r=>r.fulfill({status:200,contentType:'text/html; charset=utf-8',body:fleetHtml}));
  await pg.goto('https://fleet.probe/',{waitUntil:'domcontentloaded'});
  await pg.waitForTimeout(500);
  await pg.evaluate(()=>{ window.__relayRx({type:'prefs',inactive:[],irons:[]});
    window.__relayRx({type:'machines',machines:[{id:'clawd-atg',host:'atg',kind:'machine',online:true,lastSeen:0,stats:{projects:1,sessions:0,active:0}}]});
    handleMachineJson('clawd-atg',{type:'projects',projects:[
      {pid:'p1',name:'alpha',repoUrl:'https://github.com/clawdbotatg/alpha',kind:'gh',status:'ready',sessionCount:0,busyCount:0,waitingCount:0,created:1,pinned:false,lastTouched:100,emoji:''},
      {pid:'p2',name:'bravo',repoUrl:'https://github.com/clawdbotatg/bravo',kind:'gh',status:'ready',sessionCount:0,busyCount:0,waitingCount:0,created:1,pinned:false,lastTouched:90,emoji:''}]});
  });
  await pg.waitForTimeout(300);
  // land on alpha's sessions rung, where the "new session" composer lives
  await pg.evaluate(()=>{ location.hash = '#/p/' + encodeURIComponent(projectRows().find(p=>p.name==='alpha').id); });
  await pg.waitForTimeout(400);
};
await bootPage(page);

const cdp = await page.context().newCDPSession(page);
const micXY = await page.evaluate(()=>{ const r=micBtn.getBoundingClientRect(); return {x:r.x+r.width/2, y:r.y+r.height/2, visible:r.width>0&&r.height>0}; });
check('mic button visible on the sessions rung', micXY.visible, JSON.stringify(micXY));
check('#micBtn owns its touches (touch-action:none)',
  await page.evaluate(()=>getComputedStyle(micBtn).touchAction==='none'));
// the mic is a TAP toggle (2026-09-13): tap on, tap again to stop — holdMic /
// releaseMic are both one real tap; the names keep the acts readable.
const tapMic = async ()=>{ await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:micXY.x,y:micXY.y}]}); await page.waitForTimeout(60);
  await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]}); await page.waitForTimeout(120); };
const holdMic = tapMic, releaseMic = tapMic;

// --- 1. hold → dictate → box fills, draft saved -----------------------------
await holdMic();
check('tap starts recognition (and the finger lifting does not stop it)', await page.evaluate(()=>recOn===true && window.__sr.startedCount>=1));
await page.evaluate(()=>window.__emit(['hello world'],'again'));
check('results (final + interim) land in the box', await page.evaluate(()=>box.value==='hello world again'));
check('dictated text is saved as the draft', await page.evaluate(()=>(localStorage.getItem(draftKey(activeDraftId))||'')==='hello world again'));

// --- 2. release; a trailing final still lands in the untouched box ----------
await releaseMic();
check('second tap stops recognition', await page.evaluate(()=>recOn===false));
await page.evaluate(()=>window.__emit(['again yes'],null));
check('trailing final completes the sentence', await page.evaluate(()=>box.value==='hello world again yes'));

// --- 3. the user types → no result may ever replace the box again -----------
await page.evaluate(()=>{ box.focus(); box.setSelectionRange(box.value.length, box.value.length); });
await page.keyboard.type(' TYPED');
await page.evaluate(()=>window.__emit(['sneaky late chunk'],null));
check('late result cannot clobber typed text', await page.evaluate(()=>box.value==='hello world again yes TYPED'));

// --- 4. an edit while the mic is ON keeps it on; dictation resumes after it --
await holdMic();                                    // recording again, legitimately
await page.evaluate(()=>window.__emit(['more'],null));
check('mic dictates again after the typing round', await page.evaluate(()=>box.value.endsWith('TYPED more')));
// fix a misheard word: select "TYPED", retype it — the mic must stay lit
await page.evaluate(()=>{ box.focus(); const i=box.value.indexOf('TYPED'); box.setSelectionRange(i, i+5); });
await page.keyboard.type('FIXED');
await page.evaluate(()=>window.__emit(['next'],'trailing'));
const fixed = await page.evaluate(()=>({v:box.value, on:recOn, cls:micBtn.classList.contains('rec'), caret:box.selectionStart, at:box.value.indexOf('FIXED')+5}));
check('the edit is kept and dictation carries on after it', fixed.v==='hello world again yes FIXED more next trailing', fixed.v);
check('…the mic stayed on', fixed.on && fixed.cls, JSON.stringify(fixed));
check('…and the caret stayed where the user was editing', fixed.caret===fixed.at, JSON.stringify(fixed));
// an interim on screen at the edit is not doubled when its final lands
await page.evaluate(()=>{ box.focus(); const i=box.value.indexOf('more'); box.setSelectionRange(i, i+4); });
await page.keyboard.type('less');
await page.evaluate(()=>window.__emit(['trailing'],null));
check('interim shown at the edit lands once as its final', await page.evaluate(()=>box.value==='hello world again yes FIXED less next trailing'), await page.evaluate(()=>box.value));
await releaseMic();
check('the mic tap still stops it', await page.evaluate(()=>!recOn));
await page.evaluate(()=>{ box.focus(); box.setSelectionRange(box.value.length, box.value.length); });
await page.keyboard.type(' OFF');
await page.evaluate(()=>window.__emit(['late again'],null));
check('mic off + typed: a late result is dropped', await page.evaluate(()=>box.value.endsWith('trailing OFF')), await page.evaluate(()=>box.value));

// --- 5. switching composer context kills dictation (no cross-draft bleed) ---
await holdMic();
await page.evaluate(()=>window.__emit(['bleed one'],null));
check('mic writes into alpha before the switch', await page.evaluate(()=>box.value.endsWith('bleed one')));
await page.evaluate(()=>{ location.hash = '#/p/' + encodeURIComponent(projectRows().find(p=>p.name==='bravo').id); });
await page.waitForTimeout(400);
await page.evaluate(()=>window.__emit(['bleed two'],null));
const bled = await page.evaluate(()=>({v:box.value, on:recOn,
  alpha: localStorage.getItem(draftKey('new:p1'))||''}));
check("context switch: bravo's empty box stays empty", bled.v==='' && !bled.on, JSON.stringify({v:bled.v,on:bled.on}));
check("…and alpha's stashed draft never got 'bleed two'", bled.alpha.endsWith('bleed one'), bled.alpha);


// ---- 🎯 Deepgram engine (same touch page, back on alpha) ----------------------
await page.evaluate(()=>{ location.hash = '#/p/' + encodeURIComponent(projectRows().find(p=>p.name==='alpha').id); });
await page.waitForTimeout(400);
await page.evaluate(()=>{ box.value=''; saveDraft(); setSttWords('Codex, ethskills\nWispr Flow'); });
// 13a. creds for ANOTHER box don't count: the press falls to Web Speech
await page.evaluate(()=>{ sttCreds={proto:'token',secret:'k-other',model:'nova-3',exp:0}; sttMachine='clawd-elsewhere'; });
const srBefore = await page.evaluate(()=>window.__sr.startedCount);
await holdMic();
check("another box's creds → Web Speech at the press", await page.evaluate((n)=>recOn && recEngine==='sr' && window.__dgs.length===0 && window.__sr.startedCount>n, srBefore));
await releaseMic();
// 10. creds for THIS box → the Deepgram socket
await page.evaluate(()=>{ sttCreds={proto:'token',secret:'k-here',model:'nova-3',exp:0}; sttMachine=currentMachine; });
const relaySockets = await page.evaluate(()=>window.__sent.length);
await holdMic();
await page.waitForTimeout(150);
const dgi = await page.evaluate(()=>{ const s=window.__dgs[0]; const u=s&&new URL(s.url); return s?{on:recOn,eng:recEngine,host:u.host,path:u.pathname,proto:s.protocols,
  q:Object.fromEntries([...u.searchParams].filter(([k])=>k!=='keyterm')), terms:u.searchParams.getAll('keyterm'), tracks:window.__tracks.length}:null; });
check('hold with creds opens the Deepgram socket, engine dg', !!dgi && dgi.on && dgi.eng==='dg' && dgi.host==='api.deepgram.com' && dgi.path==='/v1/listen', JSON.stringify(dgi));
check('creds ride the subprotocol', !!dgi && JSON.stringify(dgi.proto)==='["token","k-here"]', JSON.stringify(dgi&&dgi.proto));
check('nova-3 · linear16 16k · smart_format · interim', !!dgi && dgi.q.model==='nova-3' && dgi.q.encoding==='linear16' && dgi.q.sample_rate==='16000' && dgi.q.smart_format==='true' && dgi.q.interim_results==='true', JSON.stringify(dgi&&dgi.q));
check('keyterms: your words first, then the harness names, then every project', !!dgi && dgi.terms.slice(0,3).join('|')==='Codex|ethskills|Wispr Flow' && dgi.terms.includes('clawd') && dgi.terms.includes('alpha') && dgi.terms.includes('bravo'), JSON.stringify(dgi&&dgi.terms));
check('mic track opened', !!dgi && dgi.tracks===1);
check('nothing went to the relay socket for a hold', await page.evaluate((n)=>window.__sent.length===n, relaySockets));
// 11. results through the shared guard — with the replace rules applied (built-in + the ⚙️ `=>` one)
await page.evaluate(()=>{ setSttWords('Codex, ethskills\nWispr Flow\nwhat ever => whatever'); });
await page.evaluate(()=>window.__dgResult('we are on chain with quad code what ever', false));
check('rules rewrite an interim: on chain→onchain, quad code→Claude Code, ⚙️ what ever→whatever',
  await page.evaluate(()=>box.value==='we are onchain with Claude Code whatever'), await page.evaluate(()=>box.value));
check('a rule\'s right side rides as a keyterm', await page.evaluate(()=>sttTerms().includes('whatever')));
await page.evaluate(()=>window.__dgResult('hello', false));
check('interim trails', await page.evaluate(()=>box.value==='hello'));
await page.evaluate(()=>window.__dgResult('Hello Codex', true));
await page.evaluate(()=>window.__dgResult('and', false));
check('final appends, next interim trails it', await page.evaluate(()=>box.value==='Hello Codex and'));
check('draft saved', await page.evaluate(()=>(localStorage.getItem(draftKey(activeDraftId))||'')==='Hello Codex and'));
// 12. release → CloseStream, tracks stopped; a trailing final still lands
await releaseMic();
const rel = await page.evaluate(()=>({on:recOn, close:window.__dgs[0].sent.some(d=>typeof d==='string'&&d.includes('CloseStream')), stopped:window.__tracks[0].stopped}));
check('release sends CloseStream and stops the mic', !rel.on && rel.close && rel.stopped, JSON.stringify(rel));
await page.evaluate(()=>window.__dgResult('and done', true));
check('trailing final completes the sentence', await page.evaluate(()=>box.value==='Hello Codex and done'));
await page.keyboard.type(' TYPED');
await page.evaluate(()=>window.__dgResult('sneaky', true));
check('typed text beats a late Deepgram result', await page.evaluate(()=>box.value==='Hello Codex and done TYPED'));
// 11b. an edit with the Deepgram mic ON re-bases, never stops
await holdMic();
await page.waitForTimeout(150);
await page.evaluate(()=>window.__dgResult('and', false));
await page.evaluate(()=>{ box.focus(); const i=box.value.indexOf('TYPED'); box.setSelectionRange(i, i+5); });
await page.keyboard.type('EDITED');
await page.evaluate(()=>window.__dgResult('and more', true));
const dge = await page.evaluate(()=>({v:box.value, on:recOn, eng:recEngine}));
check('Deepgram: the edit is kept, the interim is not doubled, the mic stays on', dge.v==='Hello Codex and done EDITED and more' && dge.on && dge.eng==='dg', JSON.stringify(dge));
await releaseMic();

// 11c. 2026-09-17 "it duplicates a lot of things": text PASTED after the live
// interim. Interims repeat, then the final lands — the spoken sentence must end
// up exactly once, IN PLACE, the paste after it, and new speech goes to the end.
await page.evaluate(()=>{ box.value=''; saveDraft(); });
await holdMic();
await page.waitForTimeout(150);
await page.evaluate(()=>window.__dgResult('Explain this to me', false));
await page.evaluate(()=>{ box.focus(); box.setSelectionRange(box.value.length, box.value.length); });
await page.keyboard.type('tell Eddie the state files moved and why.');
await page.evaluate(()=>window.__dgResult('Explain this to me', false));     // Deepgram re-sends an unchanged interim
await page.evaluate(()=>window.__dgResult('Explain this to me.', true));
const pasted = await page.evaluate(()=>({v:box.value, on:recOn, caret:box.selectionStart}));
check('paste after the live interim: the sentence lands once, in place, the paste after it',
  pasted.v==='Explain this to me. tell Eddie the state files moved and why.' && pasted.on, JSON.stringify(pasted));
check('…caret stays at the end of the paste', pasted.caret===pasted.v.length, JSON.stringify(pasted));
await page.evaluate(()=>window.__dgResult('and then', false));
await page.evaluate(()=>window.__dgResult('and then some.', true));
check('speech after the paste goes to the end', await page.evaluate(()=>box.value==='Explain this to me. tell Eddie the state files moved and why. and then some.'), await page.evaluate(()=>box.value));
// typing keeps going while the interim grows underneath: still no doubling
await page.evaluate(()=>window.__dgResult('one', false));
await page.evaluate(()=>{ box.focus(); box.setSelectionRange(box.value.length, box.value.length); });
await page.keyboard.type(' X');
await page.evaluate(()=>window.__dgResult('one two', false));
await page.keyboard.type('Y');
await page.evaluate(()=>window.__dgResult('one two three.', true));
check('typing while the interim grows: each spoken word once, typed text kept after',
  await page.evaluate(()=>box.value==='Explain this to me. tell Eddie the state files moved and why. and then some. one two three. XY'), await page.evaluate(()=>box.value));
// "I can just say something, hit the space bar, and I get double" (09-17):
// a space typed right after the live words, then the final + more speech
await page.evaluate(()=>window.__dgResult('control panel', false));
await page.evaluate(()=>{ box.focus(); box.setSelectionRange(box.value.length, box.value.length); });
await page.keyboard.type(' ');
await page.evaluate(()=>window.__dgResult('control panel', false));
await page.evaluate(()=>window.__dgResult('control panel.', true));
await page.evaluate(()=>window.__dgResult('next', false));
check('a space typed after the live words: they land once, the space kept, speech continues',
  await page.evaluate(()=>box.value.endsWith('XY control panel. next')), await page.evaluate(()=>JSON.stringify(box.value.slice(-40))));
await page.evaluate(()=>window.__dgResult('next.', true));
// a newline the user typed is never flattened into a space
await page.evaluate(()=>{ box.focus(); box.setSelectionRange(box.value.length, box.value.length); box.value += '\nline two'; saveDraft(); });
await page.evaluate(()=>window.__dgResult('spoken', true));
check("the user's newline survives a result", await page.evaluate(()=>box.value.endsWith('XY control panel. next.\nline two spoken')), await page.evaluate(()=>JSON.stringify(box.value.slice(-40))));
await releaseMic();

// ---- desktop page: SPACE-HOLD push-to-talk ---------------------------------
// A fresh non-emulated page: fine pointer → isTouch=false, real key events via
// CDP (keyboard.down twice = held key with repeat, exactly what a hold sends).
const dpage = await browser.newPage();
await dpage.addInitScript(initStub);
dpage.on('pageerror',e=>errors.push('desktop: '+String(e)));
await bootPage(dpage);
check('desktop page is not touch (space-hold armed)', await dpage.evaluate(()=>!isTouch));
await dpage.evaluate(()=>{ box.focus(); });

// --- 6. a quick space tap is just a space -----------------------------------
await dpage.keyboard.type('hi');
await dpage.keyboard.press(' ');
await dpage.keyboard.type('there');
await dpage.waitForTimeout(500);   // outlive the hold threshold: the tap must never fire it
check('quick tap types a normal space, no recording',
  await dpage.evaluate(()=>box.value==='hi there' && !recOn && !window.__sr));

// --- 7. hold past threshold → spaces rolled back, dictation runs ------------
await dpage.keyboard.down(' ');            // inserts a space…
await dpage.waitForTimeout(120);
await dpage.keyboard.down(' ');            // …auto-repeat inserts another…
await dpage.waitForTimeout(450);           // …then the hold threshold passes
const held = await dpage.evaluate(()=>({v:box.value, on:recOn, cls:micBtn.classList.contains('rec')}));
check('hold starts recognition and lights the mic', held.on && held.cls, JSON.stringify(held));
check('space(s) typed during the wait were rolled back', held.v==='hi there', JSON.stringify(held.v));
await dpage.evaluate(()=>window.__emit(['space talk'],null));
check('dictation lands after the rolled-back hold',
  await dpage.evaluate(()=>box.value==='hi there space talk'));

// --- 8. auto-repeat spaces are eaten while dictating ------------------------
await dpage.keyboard.down(' ');
await dpage.waitForTimeout(80);
check('repeat spaces mid-dictation do not reach the box',
  await dpage.evaluate(()=>box.value==='hi there space talk'));

// --- 9. release stops; other key during the wait cancels the pending hold ---
await dpage.keyboard.up(' ');
await dpage.waitForTimeout(250);                  // outlives SPACE_REPEAT_GRACE_MS
check('space release stops recognition', await dpage.evaluate(()=>!recOn && !micBtn.classList.contains('rec')));
check('dictated text saved as the draft', await dpage.evaluate(()=>(localStorage.getItem(draftKey(activeDraftId))||'')==='hi there space talk'));
await dpage.keyboard.down(' ');            // start a hold…
await dpage.waitForTimeout(100);
await dpage.keyboard.press('x');           // …but type through it: cancels the pending hold
await dpage.keyboard.up(' ');
await dpage.waitForTimeout(500);
check('typing during the wait cancels the hold, space kept',
  await dpage.evaluate(()=>box.value==='hi there space talk x' && !recOn));

// --- 10. Enter with the mic on: stop, wait for the tail, then send ----------
await dpage.evaluate(()=>{ box.value=''; saveDraft(); window.__sends=[]; const orig=hsend; hsend=(f)=>{ window.__sends.push(f); return orig(f); }; });
await dpage.mouse.click(await dpage.evaluate(()=>micBtn.getBoundingClientRect().x+8), await dpage.evaluate(()=>micBtn.getBoundingClientRect().y+8));
await dpage.waitForTimeout(100);
check('mic tap on desktop starts recognition', await dpage.evaluate(()=>recOn));
await dpage.evaluate(()=>window.__emit(['send this'],null));
await dpage.keyboard.press('Enter');
await dpage.waitForTimeout(50);
// (on the sessions rung a send parks in pendingSendText for the session it spawns — that's the "went out" signal here)
const mid = await dpage.evaluate(()=>({on:recOn, sent:(pendingSendText||'').includes('send this') || window.__sends.some(f=>JSON.stringify(f).includes('send this'))}));
check('Enter stops the mic and holds the send for the tail', !mid.on && !mid.sent, JSON.stringify(mid));
await dpage.evaluate(()=>window.__emit(['please'],null));   // the stop's trailing final (arrives before onend's hook fires)
await dpage.waitForTimeout(400);
const after = await dpage.evaluate(()=>({ sent:(pendingSendText||'').includes('send this please') || window.__sends.some(f=>JSON.stringify(f).includes('send this please')), box:box.value }));
check('…then sends the whole sentence, tail included', after.sent && after.box==='', JSON.stringify(after));

// ---- ⚙️ the word box: built-ins listed, and a roster frame can't rebuild it mid-word ----
await dpage.evaluate(()=>window.openSettings());
await dpage.waitForTimeout(150);
const noteTxt = await dpage.evaluate(()=>{ const n=document.querySelector('#settingsbody .setnote'); return n?n.textContent:''; });
check('word box lists the built-in words (mined baseline + project names)', /already knows \(\d+\)/.test(noteTxt) && noteTxt.includes('Codex') && noteTxt.includes('alpha'), noteTxt.slice(0,120));
await dpage.evaluate(()=>{ const ta=document.querySelector('#settingsbody .settext'); ta.focus(); ta.value=''; });
await dpage.keyboard.type('Qwi');
await dpage.evaluate(()=>window.__relayRx({type:'machines',machines:[{id:'clawd-atg',host:'atg',kind:'machine',online:true,lastSeen:0,stats:{projects:1,sessions:0,active:0}}]}));
await dpage.waitForTimeout(100);
await dpage.keyboard.type('ic');
const wb = await dpage.evaluate(()=>{ const ta=document.querySelector('#settingsbody .settext'); return {v:ta&&ta.value, focused:document.activeElement===ta, saved:localStorage.getItem('cc_stt_words')}; });
check('a machines frame mid-word keeps the box, its focus and the text', wb.v==='Qwiic' && wb.focused && wb.saved==='Qwiic', JSON.stringify(wb));
await dpage.evaluate(()=>{ document.getElementById('settingsclose').click(); });

check('no page errors', errors.length===0, errors.join(' | '));
await browser.close();
console.log(failed ? 'FAIL' : 'PASS — dictation writes only into a box it owns; an edit re-bases a live mic, beats a late result, and navigation always wins');
process.exit(failed ? 1 : 0);
