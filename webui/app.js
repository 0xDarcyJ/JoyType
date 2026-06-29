(() => {
  "use strict";
  const IMG = { '3d':'img/model_3d.webp?v=live-stick-1', top:'img/model_top.webp?v=live-stick-1' };
  const viewImage = { '3d':IMG['3d'], top:IMG.top };

  // ids match the daemon's button names; `num` is the display badge, `view` the
  // home view, `fixed` = not key-rebindable (stick is mouse-driven).
  const buttonMap = {
    STICK:   {num:'01', name:'STICK',   group:'FACE · STICK',   view:'3d', fixed:true},
    MINUS:   {num:'02', name:'MINUS',   group:'FACE · MINUS',   view:'3d'},
    UP:      {num:'03', name:'UP',      group:'FACE · D-PAD',   view:'3d'},
    DOWN:    {num:'04', name:'DOWN',    group:'FACE · D-PAD',   view:'3d'},
    LEFT:    {num:'05', name:'LEFT',    group:'FACE · D-PAD',   view:'3d'},
    RIGHT:   {num:'06', name:'RIGHT',   group:'FACE · D-PAD',   view:'3d'},
    CAPTURE: {num:'07', name:'CAPTURE', group:'FACE · CAPTURE', view:'3d'},
    LEFT_SL: {num:'08', name:'SL',      group:'RAIL · SL',      view:'3d'},
    LEFT_SR: {num:'09', name:'SR',      group:'RAIL · SR',      view:'3d'},
    L:       {num:'10', name:'L',       group:'TOP · L',        view:'top'},
    ZL:      {num:'11', name:'ZL',      group:'TOP · ZL',       view:'top'},
  };
  const hotspotLayout = {
    '3d':{ STICK:[35,30], MINUS:[56,15], UP:[40,49], DOWN:[40,63], LEFT:[32,57], RIGHT:[48,57], CAPTURE:[48,74], LEFT_SL:[86,31], LEFT_SR:[86,61] },
    top:{ L:[69,57], ZL:[30,20] },
  };
  // JoyType is a remapper for driving dictation workflows; keep the primary
  // action taxonomy tight and task-focused.
  const ACTION_TYPES = [
    {id:'dictation',name:'VOICE',icon:'voice'}, {id:'keyboard',name:'KEYS',icon:'keys'},
    {id:'window',name:'WINDOW',icon:'window'},
  ];
  const TRIGGERS = [
    {id:'press',name:'PRESS'}, {id:'double',name:'DOUBLE'}, {id:'hold',name:'HOLD'},
  ];
  const DEFAULT_DOUBLE_MS = 280, DEFAULT_HOLD_MS = 350;
  const PROFILE_DISP = {default:'DEFAULT', vscode:'VS CODE', terminal_cli:'TERMINAL CLI'};
  const disp = n => PROFILE_DISP[n] || n.toUpperCase();
  const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const layerLeaf = n => String(n||'').split('::').pop();
  const layerDisplay = n => {
    const d = DETAILS[n] || {};
    const key = d.displayName || layerLeaf(n);
    return PROFILE_DISP[key] || String(key||'').replace(/_/g,' ').toUpperCase();
  };
  const baseOf = n => {
    const d = DETAILS[n] || {};
    return d.base || n || DEFAULT_KEY;
  };
  const isBaseLayer = n => {
    const d = DETAILS[n] || {};
    return !!d.isBase || !d.base;
  };
  const isOverrideLayer = n => {
    const d = DETAILS[n] || {};
    return !!d.isOverride || !!d.base;
  };
  const appScope = d => {
    const ps=(d&&d.match&&d.match.process)||[];
    if(!ps.length) return 'NO APP MATCH';
    return ps.length>2 ? ps.slice(0,2).join(', ').toUpperCase()+' +'+(ps.length-2) : ps.join(', ').toUpperCase();
  };

  // ---- state ----
  let DETAILS = {};          // {profile: {isDefault, match, bindings(raw)}}
  let DEFAULT_KEY = 'default';
  let order = [];            // profile names, default first
  let collapsedBases = {};   // {baseName: true}
  let editTarget = 'default';
  let selected = 'LEFT_SL';
  let selectedTrigger = 'press';
  let inspectorTab = 'action';
  let view = '3d', zoom = 1;
  let EDIT = {type:'keyboard', keys:[]};   // in-progress action for `selected`
  let recording = false;
  let recCb = null, recLeftMods = false;  // capture target; left-side mods for voice/IME
  let creating = false;      // false | 'profile' | 'override'
  let VOICE_HK = {hold:[], toggle:[]};  // dictation-IME hotkeys (global)
  let MOUSE_CFG = {acceleration:2.5};
  let HAPTICS = {click:[], strength:'medium'};
  let HAPTIC_EDIT = {clickEnabled:false, strength:'medium'};
  let stickPreviewRunning = false, stickPreviewFrame = 0, stickPreviewLast = 0;
  const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];

  // ---- scale-to-fit ----
  const DESIGN_W = 1600, DESIGN_H = 900;
  let fitFrame = 0, lastFitW = 0, lastFitH = 0, lastFitTransform = '';
  function fit(){
    const w = document.documentElement.clientWidth, h = document.documentElement.clientHeight;
    if (w<2||h<2) return;
    const scale = Math.min(w/DESIGN_W,h/DESIGN_H);
    const left = Math.round((w-DESIGN_W*scale)/2);
    const top = Math.round((h-DESIGN_H*scale)/2);
    const transform = `translate(${left}px,${top}px) scale(${scale})`;
    if (w===lastFitW && h===lastFitH && transform===lastFitTransform) return;
    lastFitW = w; lastFitH = h;
    lastFitTransform = transform;
    $('#app').style.transform=transform;
  }
  function requestFit(){
    if(fitFrame) return;
    fitFrame = requestAnimationFrame(()=>{ fitFrame = 0; fit(); });
  }
  addEventListener('resize', requestFit); addEventListener('load', requestFit);
  requestFit();

  let toastTimer;
  function toast(msg){ const t=$('#toast'); t.textContent=msg; t.classList.add('show');
    clearTimeout(toastTimer); toastTimer=setTimeout(()=>t.classList.remove('show'),1700); }

  // ---- action <-> label / editor ----
  function keyLabel(k){
    k=String(k); const low=k.toLowerCase();
    const a={up:'UP',down:'DOWN',left:'LEFT',right:'RIGHT',esc:'ESC',escape:'ESC',
      ctrl:'CTRL',control:'CTRL',shift:'SHIFT',alt:'ALT',win:'WIN',lwin:'LWIN',
      enter:'ENTER',return:'ENTER',tab:'TAB',space:'SPACE'}[low];
    return a||k.toUpperCase();
  }
  function keyText(keys, fallback){
    const k=[].concat(keys||[]).filter(Boolean);
    return k.length ? k.map(keyLabel).join(' + ') : fallback;
  }
  function modeIcon(id){
    const icons={
      dictation:'<svg class="mode-glyph" aria-hidden="true" viewBox="0 0 40 40"><rect x="15" y="5" width="10" height="19" rx="5"></rect><path d="M10 19v2c0 6 4 10 10 10s10-4 10-10v-2"></path><path d="M20 31v5"></path><path d="M14 36h12"></path></svg>',
      keyboard:'<svg class="mode-glyph" aria-hidden="true" viewBox="0 0 40 40"><rect x="8" y="11" width="24" height="18" rx="1"></rect><path d="M13 17h14"></path><path d="M13 23h14"></path></svg>',
      window:'<svg class="mode-glyph" aria-hidden="true" viewBox="0 0 40 40"><rect x="8" y="10" width="17" height="14"></rect><rect x="15" y="16" width="17" height="14"></rect></svg>'
    };
    return icons[id]||icons.window;
  }
  function bindPseudoButton(el, fn){
    el.addEventListener('click',fn);
    el.addEventListener('keydown',e=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); fn(e); } });
  }
  function lowerKeys(keys){ return [].concat(keys||[]).map(k=>String(k).toLowerCase()); }
  function windowDirectionFromAction(raw){
    if(!raw || !('hotkey' in raw)) return null;
    const keys=lowerKeys(raw.hotkey);
    const hasTab=keys.includes('tab');
    const hasAlt=keys.some(k=>['alt','lalt','ralt'].includes(k));
    const hasShift=keys.some(k=>['shift','lshift','rshift'].includes(k));
    const allowed=keys.every(k=>['alt','lalt','ralt','shift','lshift','rshift','tab'].includes(k));
    if(!hasTab || !hasAlt || !allowed) return null;
    if(keys.length===2 && !hasShift) return 'forward';
    if(keys.length===3 && hasShift) return 'reverse';
    return null;
  }
  function normalizeHold(raw){
    if(!raw) return null;
    if(raw.action) return {action:raw.action, after_ms:Number(raw.after_ms||DEFAULT_HOLD_MS), on_release:!!raw.on_release};
    return {action:raw, after_ms:DEFAULT_HOLD_MS, on_release:false};
  }
  function splitBinding(raw){
    const out={press:null,double:null,hold:null,double_ms:DEFAULT_DOUBLE_MS};
    if(!raw) return out;
    if(raw.triggers){
      const t=raw.triggers||{};
      out.press=t.press||null; out.double=t.double||null; out.hold=normalizeHold(t.hold);
      out.double_ms=Number(t.double_ms||DEFAULT_DOUBLE_MS);
      return out;
    }
    out.press=raw;
    return out;
  }
  function actionForTrigger(raw,trig){
    const b=splitBinding(raw);
    if(trig==='hold') return b.hold ? b.hold.action : null;
    return b[trig] || null;
  }
  function isPushToTalkAction(action){ return !!(action && action.voice===true && Object.keys(action).length===1); }
  function triggerHasAction(raw,trig){ return !!actionForTrigger(raw,trig); }
  function bindingToRaw(b){
    const hasDouble=!!b.double, hasHold=!!b.hold;
    const hasCustomDoubleMs=Number(b.double_ms||DEFAULT_DOUBLE_MS)!==DEFAULT_DOUBLE_MS;
    if(!hasDouble && !hasHold && !hasCustomDoubleMs) return b.press || null;
    const t={};
    if(b.press) t.press=b.press;
    if(b.double) t.double=b.double;
    if(hasHold) t.hold={after_ms:Number(b.hold.after_ms||DEFAULT_HOLD_MS), on_release:!!b.hold.on_release, action:b.hold.action};
    if(hasCustomDoubleMs) t.double_ms=Number(b.double_ms);
    return {triggers:t};
  }
  function applyTuneTimingToBinding(next){
    next.double_ms=Number(EDIT.doubleMs||DEFAULT_DOUBLE_MS);
    if(next.hold){
      next.hold={
        after_ms:Number(EDIT.holdMs||DEFAULT_HOLD_MS),
        on_release:!!EDIT.holdOnRelease,
        action:next.hold.action,
      };
    }
  }
  function editForTrigger(raw,trig){
    const parts=splitBinding(raw);
    const ed=actionToEdit(actionForTrigger(raw,trig));
    const h=parts.hold;
    ed.doubleMs=Number(parts.double_ms||DEFAULT_DOUBLE_MS);
    ed.holdMs=h ? Number(h.after_ms||DEFAULT_HOLD_MS) : DEFAULT_HOLD_MS;
    ed.holdOnRelease=h ? !!h.on_release : false;
    return ed;
  }
  function actionTypeLabel(raw){
    if(!raw) return '—';
    if(windowDirectionFromAction(raw)) return 'WINDOW';
    if('key' in raw || 'hotkey' in raw) return 'KEYBOARD';
    if(raw.voice===true || raw.voice_toggle===true) return 'DICTATION';
    return 'UNKNOWN';
  }
  function labelOf(raw){
    if(!raw) return '—';
    if(raw.triggers){
      const b=splitBinding(raw), parts=[];
      if(b.press) parts.push('P '+labelOf(b.press));
      if(b.double) parts.push('D '+labelOf(b.double));
      if(b.hold) parts.push('H '+labelOf(b.hold.action));
      return parts.length ? parts.join(' / ') : '—';
    }
    if('key' in raw) return keyLabel(raw.key);
    const windowDir=windowDirectionFromAction(raw);
    if(windowDir) return windowDir==='reverse' ? 'REVERSE' : 'FORWARD';
    if('hotkey' in raw) return [].concat(raw.hotkey).map(keyLabel).join('+');
    if(raw.voice===true) return 'PUSH TO TALK';
    if(raw.voice_toggle===true) return 'DICTATION';
    return '?';
  }

  function actionToEdit(raw){
    if(!raw) return {type:'keyboard', keys:[]};
    if('key' in raw) return {type:'keyboard', keys:[String(raw.key)]};
    const windowDir=windowDirectionFromAction(raw);
    if(windowDir) return {type:'window', direction:windowDir};
    if('hotkey' in raw) return {type:'keyboard', keys:[].concat(raw.hotkey).map(String)};
    if(raw.voice===true) return {type:'dictation', voice:'hold'};
    if(raw.voice_toggle===true) return {type:'dictation', voice:'toggle'};
    return {type:'keyboard', keys:[]};
  }
  function editToAction(ed){
    if(ed.type==='keyboard'){ const k=ed.keys||[]; if(!k.length) return null; return k.length===1?{key:k[0]}:{hotkey:k}; }
    if(ed.type==='dictation') return ed.voice==='toggle'?{voice_toggle:true}:{voice:true};
    if(ed.type==='window') return ed.direction==='reverse'?{hotkey:['shift','alt','tab']}:{hotkey:['alt','tab']};
    return null;
  }
  function canonicalAction(raw){
    if(!raw) return null;
    if(raw.triggers){
      const b=splitBinding(raw), t={};
      if(b.press) t.press=canonicalAction(b.press);
      if(b.double) t.double=canonicalAction(b.double);
      if(b.hold) t.hold={after_ms:Number(b.hold.after_ms||DEFAULT_HOLD_MS), on_release:!!b.hold.on_release, action:canonicalAction(b.hold.action)};
      if(b.double_ms && Number(b.double_ms)!==DEFAULT_DOUBLE_MS) t.double_ms=Number(b.double_ms);
      return {triggers:t};
    }
    const windowDir=windowDirectionFromAction(raw);
    if(windowDir) return windowDir==='reverse'
      ? {hotkey:['shift','alt','tab']}
      : {hotkey:['alt','tab']};
    if('hotkey' in raw) return {hotkey:[].concat(raw.hotkey).map(String)};
    if('key' in raw) return {key:String(raw.key)};
    return raw;
  }
  function sameBinding(a,b){ return JSON.stringify(canonicalAction(a))===JSON.stringify(canonicalAction(b)); }
  function editLabel(ed){
    if(ed.type==='window') return ed.direction==='reverse' ? 'REVERSE' : 'FORWARD';
    const a=editToAction(ed); return a?labelOf(a):'—';
  }
  function normalizeStrength(v){
    const s=String(v||'medium').toLowerCase();
    if(s==='light' || s==='low') return 'light';
    if(s==='strong' || s==='high') return 'strong';
    return 'medium';
  }
  function normalizeHaptics(raw){
    const cfg=raw||{};
    const click=[...new Set([].concat(cfg.click||[]).map(String).filter(Boolean))];
    return {click, strength:normalizeStrength(cfg.strength)};
  }
  function resetHapticEdit(){
    const cfg=normalizeHaptics(HAPTICS);
    HAPTIC_EDIT={clickEnabled:cfg.click.includes(selected), strength:cfg.strength};
  }
  function hapticDraftConfig(){
    const click=(HAPTICS.click||[]).filter(k=>k!==selected);
    if(HAPTIC_EDIT.clickEnabled) click.push(selected);
    return normalizeHaptics({click, strength:HAPTIC_EDIT.strength});
  }
  function sameClickList(a,b){
    const aa=[...new Set([].concat(a||[]).map(String))].sort();
    const bb=[...new Set([].concat(b||[]).map(String))].sort();
    return JSON.stringify(aa)===JSON.stringify(bb);
  }
  function hapticsDirty(){
    if(buttonMap[selected] && buttonMap[selected].fixed) return false;
    const cur=normalizeHaptics(HAPTICS), next=hapticDraftConfig();
    return !sameClickList(cur.click,next.click) || cur.strength!==next.strength;
  }
  function actionDirty(){
    if(buttonMap[selected] && buttonMap[selected].fixed) return false;
    const composed=composedBindingForEdit(), cur=effectiveRaw(selected);
    return composed!==null && !sameBinding(composed, cur===undefined?null:cur);
  }

  // ---- binding resolution (inheritance) ----
  function ownRaw(profile, btn){ const d=DETAILS[profile]; return d && d.bindings ? d.bindings[btn] : undefined; }
  function effectiveRaw(btn){
    const o = ownRaw(editTarget, btn);
    if(o !== undefined) return o;
    return ownRaw(baseOf(editTarget), btn);
  }
  function isOverride(btn){ return isOverrideLayer(editTarget) && ownRaw(editTarget,btn)!==undefined; }
  function bindStateOf(btn){
    if(buttonMap[btn] && buttonMap[btn].fixed) return 'MOUSE';
    if(isBaseLayer(editTarget)) return 'BASE';
    return isOverride(btn) ? 'OVERRIDE' : 'INHERITED';
  }
  function bindingFor(btn){ if(buttonMap[btn] && buttonMap[btn].fixed) return 'MOUSE'; return labelOf(effectiveRaw(btn)); }
  function buttonZone(d){
    const raw=(d && d.group ? d.group : '').split(' · ')[0];
    return raw==='LEFT' ? 'D-PAD' : raw;
  }

  // ---- render: header / status ----
  function renderHeaderProfile(){
    const ph=$('#profileHeader'); if(ph) ph.textContent = '';
    const eh=$('#editingHint');
    if(eh){
      if(isOverrideLayer(editTarget)) eh.textContent = layerDisplay(baseOf(editTarget))+' · '+layerDisplay(editTarget)+' OVERRIDE';
      else eh.textContent = layerDisplay(editTarget)+' · BASE LAYER';
    }
  }
  function batteryBars(value){
    const raw = Number(value) || 0;
    if(raw <= 0) return 0;
    if(raw <= 4) return Math.max(1, Math.min(5, Math.round(raw * 1.25)));
    return Math.max(0, Math.min(5, Math.round(raw)));
  }
  function setDeviceStatus(mode,batteryLevel,charging){
    const led=$('#connLed'), battery=$('#batteryMeter'), status=$('#deviceStatus');
    const level=batteryBars(batteryLevel);
    if(led) led.className='device-led '+mode;
    if(battery){
      battery.className='battery-meter level-'+level+(charging?' charging':'');
      battery.setAttribute('aria-label',(charging?'Charging, ':'')+'battery level '+level+' of 5');
    }
    if(status){
      const label = mode==='connected' ? 'Joy-Con connected' : mode==='searching' ? 'Searching for Joy-Con' : 'Joy-Con offline';
      status.setAttribute('aria-label',label);
      status.title=label;
    }
  }
  // exactly three states, so the user always knows if it's usable
  function applyStatus(s){
    if(s.connected){
      setDeviceStatus('connected',{4:4,3:3,2:2,1:1,0:0}[s.battery]??0,!!s.charging);
    } else if(s.running){
      setDeviceStatus('searching',0,false);
    } else {
      setDeviceStatus('offline',0,false);
    }
  }

  // ---- render: profile list + match editor ----
  function renderProfiles(){
    const list=$('#profileList'); list.innerHTML='';
    const bases = order.filter(n=>DETAILS[n] && isBaseLayer(n));
    const seen = new Set();
    const draw=(n,idx,kind)=>{
      const d=DETAILS[n]||{bindings:{}};
      const ocount = Object.keys(d.bindings||{}).length;
      const isBase = kind==='base';
      const sub = isBase ? 'BASE LAYER' : appScope(d);
      const collapsed = isBase && !!collapsedBases[n];
      const tag = isBase ? '<span class="profile-tag base"><b>BASE</b><small>LAYER</small></span>'
                         : `<span class="profile-tag ovr"><b>${ocount}</b><small>OVR</small></span>`;
      const b=document.createElement('button');
      b.className='profile profile-'+kind+(n===editTarget?' active':'')+(n===DEFAULT_KEY?' is-default':'')+(collapsed?' collapsed':'');
      if(isBase) b.setAttribute('aria-expanded', collapsed?'false':'true');
      b.innerHTML=`<span class="profile-no">${idx ? String(idx).padStart(2,'0') : ''}</span>`
        +`<span><span class="profile-name">${esc(layerDisplay(n))}</span><span class="profile-process">${esc(sub)}</span></span>`
        +tag;
      b.addEventListener('click',e=>{
        if(isBase && e.target.closest('.profile-tag')){
          collapsedBases[n]=!collapsedBases[n];
          renderProfiles();
          return;
        }
        creating=false; loadTarget(n); toast('EDITING '+layerDisplay(n));
      });
      list.appendChild(b);
    };
    bases.forEach(base=>{
      if(seen.has(base)) return;
      seen.add(base);
      const baseIndex = bases.indexOf(base) + 1;
      draw(base, baseIndex, 'base');
      if(collapsedBases[base]) return;
      order.filter(n=>baseOf(n)===base && n!==base).forEach(child=>{
        seen.add(child);
        draw(child, '', 'override');
      });
    });
    order.filter(n=>!seen.has(n)).forEach(n=>{
      draw(n, isOverrideLayer(n)?'':bases.length+1, isOverrideLayer(n)?'override':'base');
    });
    $('#profileCount').textContent=String(bases.length).padStart(2,'0');
  }

  function fillProcFromExe(inputId){
    bridge.pickExe(stem=>{ if(!stem) return; const inp=document.getElementById(inputId); if(!inp) return;
      const cur=inp.value.split(',').map(s=>s.trim()).filter(Boolean);
      if(!cur.includes(stem)) cur.push(stem); inp.value=cur.join(', '); });
  }
  function renderMatchEditor(){
    const box=$('#matchEditor'); if(!box) return;
    if(creating){
      const isAppOverride = creating==='override';
      const base = baseOf(editTarget);
      box.innerHTML =
        `<div class="me-title">${isAppOverride?'NEW OVERRIDE':'NEW PROFILE'}</div>
         <div class="me-row"><label>NAME</label><input id="npName" placeholder="e.g. notepad" autocomplete="off"></div>
         ${isAppOverride?'<div class="me-row"><label>PROCESS</label><input id="npProc" placeholder="e.g. notepad" autocomplete="off"></div><div class="me-browserow"><button class="me-btn ghost" id="npBrowse">BROWSE .EXE…</button></div>':''}
         <div class="me-actions"><button class="me-btn" id="npCancel">CANCEL</button><button class="me-btn go" id="npCreate">CREATE</button></div>`;
      if(isAppOverride) $('#npBrowse').addEventListener('click',()=>fillProcFromExe('npProc'));
      $('#npCreate').addEventListener('click',()=>{
        const name=($('#npName').value||'').trim().toLowerCase().replace(/[^a-z0-9_]/g,'_');
        if(!name) return toast('NAME REQUIRED');
        if(isAppOverride){
          const proc=($('#npProc').value||'').split(',').map(s=>s.trim()).filter(Boolean);
          if(!proc.length) return toast('PROCESS REQUIRED');
          call('createOverride',[base, name, JSON.stringify({process:proc, process_icase:true})], ()=>{ creating=false; editTarget=base+'::'+name; toast('OVERRIDE CREATED'); });
        } else {
          call('createBaseProfile',[name], ()=>{ creating=false; editTarget=name; toast('PROFILE CREATED'); });
        }
      });
      $('#npCancel').addEventListener('click',()=>{ creating=false; renderMatchEditor(); });
      return;
    }
    if(isBaseLayer(editTarget)){
      box.innerHTML =
        `<div class="me-title">BASE PROFILE</div>
         <div class="me-row"><label>NAME</label><input id="bpName" value="${esc(layerDisplay(editTarget))}" autocomplete="off"></div>
         <div class="me-actions"><button class="me-btn go" id="bpSave">SAVE PROFILE</button></div>`;
      $('#bpSave').addEventListener('click',()=>{
        const name=($('#bpName').value||'').trim();
        if(!name) return toast('NAME REQUIRED');
        call('setProfileDisplayName',[editTarget, name], ()=>toast('PROFILE SAVED'));
      });
      return;
    }
    const d=DETAILS[editTarget]||{}; const m=d.match||{process:[],title_regex:''};
    box.innerHTML =
      `<div class="me-title">APP MATCH · ${layerDisplay(editTarget)}</div>
       <div class="me-row"><label>PROCESS</label><input id="meProc" value="${(m.process||[]).join(', ')}" placeholder="e.g. code" autocomplete="off"></div>
       <div class="me-browserow"><button class="me-btn ghost" id="meBrowse">BROWSE .EXE…</button></div>
       <div class="me-row"><label>TITLE</label><input id="meTitle" value="${m.title_regex||''}" placeholder="optional regex" autocomplete="off"></div>
       <div class="me-actions"><button class="me-btn danger" id="meDelete">DELETE</button><button class="me-btn go" id="meSave">SAVE MATCH</button></div>`;
    $('#meBrowse').addEventListener('click',()=>fillProcFromExe('meProc'));
    $('#meSave').addEventListener('click',()=>{
      const proc=($('#meProc').value||'').split(',').map(s=>s.trim()).filter(Boolean);
      const title=($('#meTitle').value||'').trim();
      if(!proc.length && !title) return toast('MATCH REQUIRED');
      const match={process:proc, process_icase:true}; if(title) match.title_regex=title;
      call('setMatch',[editTarget, JSON.stringify(match)], ()=>toast('MATCH SAVED'));
    });
    $('#meDelete').addEventListener('click',()=>{
      const t=editTarget;
      call('deleteProfile',[t], ()=>{ editTarget=DEFAULT_KEY; toast('PROFILE DELETED'); });
    });
  }

  // ---- render: model / hotspots ----
  function renderView(){
    $$('.view-btn').forEach(b=>b.classList.toggle('active',b.dataset.view===view));
    $('#modelImage').src=viewImage[view]; $('#modelWrap').className=`view-${view}`;
    $('#modelWrap').style.setProperty('--zoom',zoom);
    renderHotspots();
  }
  function renderHotspots(){
    const root=$('#hotspots'); root.innerHTML='';
    const layout=hotspotLayout[view]||{};
    Object.entries(layout).forEach(([id,pos])=>{ const d=buttonMap[id]; if(!d) return;
      const b=document.createElement('button');
      b.className='hotspot hotspot-'+id.toLowerCase()+(selected===id?' selected':'')+(isOverride(id)?' ovr':'');
      b.style.left=`calc(${pos[0]}% - 18px)`; b.style.top=`calc(${pos[1]}% - 18px)`;
      b.dataset.name=d.name; b.textContent=d.num; b.title=d.name;
      b.addEventListener('click',e=>{ e.stopPropagation(); selectButton(id); });
      root.appendChild(b);
    });
  }

  // ---- render: inspector (the editor) ----
  function renderCallout(){
    const d=buttonMap[selected];
    const ov=$('#overrideState'); if(ov) ov.textContent = bindStateOf(selected)+' · '+d.name;
  }
  function assignmentDisplayRaw(){
    if(buttonMap[selected] && buttonMap[selected].fixed) return null;
    const raw = effectiveRaw(selected);
    const composed = composedBindingForEdit();
    if(composed && !sameBinding(composed, raw===undefined?null:raw)) return composed;
    return raw;
  }
  function renderAssignmentRows(){
    const row=$('#assignmentRows'); if(!row) return;
    row.innerHTML='';
    const raw=assignmentDisplayRaw();
    const pttExclusive=isPushToTalkAction(raw);
    TRIGGERS.forEach(t=>{
      const action=actionForTrigger(raw,t.id);
      const locked=pttExclusive && t.id!=='press';
      const b=document.createElement('button');
      b.type='button';
      b.className='assignment-row'+(selectedTrigger===t.id?' active':'')+(action?' bound':'')+(locked?' locked':'');
      b.disabled=locked;
      b.innerHTML='<span class="asg-dot"></span><span class="asg-trigger">'+t.name+'</span>';
      b.addEventListener('click',()=>{
        if(locked) return;
        const pending=composedBindingForEdit();
        selectedTrigger=t.id;
        EDIT=editForTrigger(pending||effectiveRaw(selected), selectedTrigger);
        recording=false;
        renderInspector();
      });
      row.appendChild(b);
    });
  }
  function renderMouseEditor(){
    const box=$('#valueEditor'); if(!box) return;
    const accel=Number(MOUSE_CFG.acceleration||1);
    const accelText=accel.toFixed(1)+'x';
    const sliderMin=0.2, sliderMax=3.0;
    const sliderPct=Math.max(0, Math.min(100, ((accel-sliderMin)/(sliderMax-sliderMin))*100));
    setOutput('MOUSE');
    box.innerHTML='<div class="stick-panel">'
      +'<div class="stick-axis" aria-hidden="true"><i class="axis-x"></i><i class="axis-y"></i><i class="axis-ring"></i><i class="axis-dot" id="axisDot"></i><span class="axis-label axis-label-x">X</span><span class="axis-label axis-label-y">Y</span></div>'
      +'<div class="stick-tune"><div class="mouse-readout"><span>SENSITIVITY</span><strong id="mouseAccelValue">'+accelText+'</strong></div>'
      +'<input class="mouse-slider" id="mouseAccel" type="range" min="'+sliderMin+'" max="'+sliderMax+'" step="0.1" value="'+accel+'" style="--mouse-pct:'+sliderPct+'%">'
      +'<div class="mouse-scale"><span>PRECISE</span><span>QUICK</span></div></div>'
      +'</div>';
    const slider=$('#mouseAccel'), readout=$('#mouseAccelValue');
    if(slider) slider.addEventListener('input',()=>{
      const value=Number(slider.value||1);
      const fill=Math.max(0, Math.min(100, ((value-sliderMin)/(sliderMax-sliderMin))*100));
      MOUSE_CFG.acceleration=value;
      slider.style.setProperty('--mouse-pct', fill+'%');
      if(readout) readout.textContent=value.toFixed(1)+'x';
      if(bridge.setMouseAcceleration) bridge.setMouseAcceleration(value,()=>{});
      updateActionButtons();
    });
    applyStickPreview({left:{x:0,y:0,magnitude:0}});
  }
  function applyStickPreview(snapshot){
    const dot=$('#axisDot'); if(!dot) return;
    const stick=(snapshot&&snapshot.left)||{x:0,y:0,magnitude:0};
    const clamp=v=>Math.max(-1, Math.min(1, Number(v)||0));
    const x=clamp(stick.x), y=clamp(stick.y);
    dot.style.left=(50 + x * 42)+'%';
    dot.style.top=(50 - y * 42)+'%';
    dot.style.setProperty('--stick-mag', Math.max(0, Math.min(1, Number(stick.magnitude)||0)));
  }
  function pollStickPreview(ts){
    if(!stickPreviewRunning){ stickPreviewFrame=0; return; }
    if(!stickPreviewLast || ts-stickPreviewLast>=33){
      stickPreviewLast=ts;
      if(bridge.getStickState){
        bridge.getStickState(j=>{ try{ applyStickPreview(JSON.parse(j)); }catch(e){} });
      }
    }
    stickPreviewFrame=requestAnimationFrame(pollStickPreview);
  }
  function startStickPreview(){
    stickPreviewRunning=true;
    if(!stickPreviewFrame) stickPreviewFrame=requestAnimationFrame(pollStickPreview);
  }
  function stopStickPreview(){
    stickPreviewRunning=false;
    stickPreviewLast=0;
    if(stickPreviewFrame){ cancelAnimationFrame(stickPreviewFrame); stickPreviewFrame=0; }
  }
  function renderInspector(){
    const d=buttonMap[selected]; if(!d) return;
    const inspector=document.querySelector('.inspector');
    if(inspector) inspector.classList.toggle('stick-inspector', !!d.fixed);
    const stateSection=$('#stateSection');
    const tuneLayout=inspectorTab==='tune' && !d.fixed;
    if(stateSection) stateSection.classList.toggle('tune-state', tuneLayout);
    if(inspector) inspector.classList.toggle('tune-inspector', tuneLayout);
    const selectedTitle=document.querySelector('#selectedSection .section-title span');
    if(selectedTitle) selectedTitle.textContent=d.fixed?'STICK':'BUTTON';
    $('#inspectNo').textContent=d.num;
    $('#inspectName').textContent=d.name;
    $('#inspectGroup').textContent=buttonZone(d);
    const bs=bindStateOf(selected); const bsEl=$('#bindState'); bsEl.textContent=bs;
    bsEl.className='selected-word state-'+bs.toLowerCase();

    if(d.fixed){
      hideInspectorTabs();
      hideStateTunePanel();
      $('#actionGrid').innerHTML='';
      $('#assignmentSection').style.display='none';
      $('#modeSection').style.display='none';
      $('#behaviorSection').style.display='none';
      $('#actionsSection').style.display='';
      $('#commandSection').style.display='';
      $('#commandSection').className='inspector-section command-section command-mode-mouse';
      $('#valueLabel').textContent='POINTER CONTROL';
      renderMouseEditor();
      startStickPreview();
      updateActionButtons();
      return;
    }
    stopStickPreview();
    renderInspectorTabs();
    if(isPushToTalkAction(effectiveRaw(selected))) selectedTrigger='press';
    $('#actionsSection').style.display='';
    setOutput(editLabel(EDIT));

    if(inspectorTab==='tune') renderTuneEditor();
    else renderActionEditor();
    updateActionButtons();
  }

  function renderInspectorTabs(){
    const inspector=document.querySelector('.inspector'); if(!inspector) return;
    let tabs=$('#inspectorTabs');
    if(!tabs){
      tabs=document.createElement('div');
      tabs.id='inspectorTabs';
      tabs.className='inspector-tabs';
      inspector.insertBefore(tabs,$('#commandSection'));
    }
    tabs.style.display='';
    tabs.innerHTML='';
    [['action','ACTION'],['tune','TUNE']].forEach(([id,label])=>{
      const b=document.createElement('button');
      b.type='button';
      b.className='inspector-tab'+(inspectorTab===id?' active':'');
      b.textContent=label;
      b.addEventListener('click',()=>{
        if(inspectorTab===id) return;
        inspectorTab=id;
        recording=false;
        renderInspector();
      });
      tabs.appendChild(b);
    });
  }
  function hideInspectorTabs(){
    const tabs=$('#inspectorTabs');
    if(tabs) tabs.style.display='none';
  }
  function stateTunePanel(){
    const stateSection=$('#stateSection'); if(!stateSection) return null;
    let panel=$('#stateTunePanel');
    if(!panel){
      panel=document.createElement('div');
      panel.id='stateTunePanel';
      panel.className='state-block state-tune-panel';
      stateSection.appendChild(panel);
    }
    return panel;
  }
  function hideStateTunePanel(){
    const panel=$('#stateTunePanel');
    if(panel) panel.style.display='none';
  }
  function setSectionTitle(id,label){
    const el=document.querySelector(id+' .section-title span');
    if(el) el.textContent=label;
  }
  function renderActionEditor(){
    hideStateTunePanel();
    setSectionTitle('#assignmentSection','TRIGGERS');
    setSectionTitle('#modeSection','MODE');
    $('#assignmentSection').style.display='';
    $('#modeSection').style.display='';
    $('#behaviorSection').style.display='none';
    $('#commandSection').style.display='';
    const behaviorBox=$('#behaviorEditor'); if(behaviorBox) behaviorBox.innerHTML='';
    const grid=$('#actionGrid'); grid.innerHTML='';
    ACTION_TYPES.forEach(t=>{ const b=document.createElement('button');
      b.className='type-key'+(EDIT.type===t.id?' active':'');
      b.innerHTML='<span class="mode-icon">'+modeIcon(t.id)+'</span><span class="mode-name">'+t.name+'</span>';
      b.addEventListener('click',()=>{ setType(t.id); }); grid.appendChild(b);
    });
    renderAssignmentRows();
    renderValueEditor();
  }

  function renderTuneEditor(){
    recording=false;
    setSectionTitle('#assignmentSection','TRIGGERS');
    setSectionTitle('#modeSection','MODE');
    $('#assignmentSection').style.display='none';
    $('#modeSection').style.display='none';
    $('#behaviorSection').style.display='none';
    $('#commandSection').style.display='none';
    $('#assignmentRows').innerHTML='';
    $('#actionGrid').innerHTML='';
    const behaviorBox=$('#behaviorEditor'); if(behaviorBox) behaviorBox.innerHTML='';
    setOutput('TUNE');
    const panel=stateTunePanel(); if(!panel) return;
    panel.style.display='';
    const strength=normalizeStrength(HAPTIC_EDIT.strength);
    panel.innerHTML='<div class="tune-panel"><div class="tune-zone tune-haptic">'
      +'<button class="tune-switch haptic-click'+(HAPTIC_EDIT.clickEnabled?' active':'')+'" id="hapticClick" type="button">'
      +'<span class="tune-dot"></span><span><strong>HAPTIC CLICK</strong><em>'+buttonMap[selected].name+' SELECTED</em></span><b>'+(HAPTIC_EDIT.clickEnabled?'ON':'OFF')+'</b></button>'
      +'<div class="tune-strength"><span class="tune-field-label">STRENGTH</span><div class="strength-strip" id="hapticStrength"></div></div>'
      +'</div><div class="tune-zone tune-timing"><div class="tune-zone-head"><strong>GESTURE TIMING</strong><em>DOUBLE / HOLD RESPONSE</em></div><div class="tune-timing-editor" id="tuneTimingEditor"></div></div>'
      +'</div>';
    $('#hapticClick').addEventListener('click',()=>{
      HAPTIC_EDIT.clickEnabled=!HAPTIC_EDIT.clickEnabled;
      renderTuneEditor();
      updateActionButtons();
    });
    const strip=$('#hapticStrength');
    [['light','LIGHT'],['medium','MED'],['strong','STRONG']].forEach(([id,label])=>{
      const b=document.createElement('button');
      b.type='button';
      b.className='strength-key'+(strength===id?' active':'');
      b.textContent=label;
      b.addEventListener('click',()=>{
        HAPTIC_EDIT.strength=id;
        renderTuneEditor();
        updateActionButtons();
      });
      strip.appendChild(b);
    });
    renderBehaviorEditor('#tuneTimingEditor');
  }

  function chip(label, active, on){ const b=document.createElement('button');
    b.className='val-chip'+(active?' active':''); b.textContent=label; b.addEventListener('click',on); return b; }
  function commandStage(kind){
    const stage=document.createElement('div');
    stage.className='command-stage '+kind+'-stage';
    return stage;
  }
  function renderValueEditor(){
    const box=$('#valueEditor'), prompt=$('#commandPrompt');
    box.innerHTML='';
    const t=EDIT.type;
    const commandSection=$('#commandSection');
    if(commandSection) commandSection.className='inspector-section command-section command-mode-'+t+' command-action-only';
    const behaviorSection=$('#behaviorSection');
    if(behaviorSection) behaviorSection.style.display='none';
    $('#valueLabel').textContent='COMMAND';
    setOutput(recording ? 'WAITING' : editLabel(EDIT));
    if(prompt) prompt.textContent=recording?'PRESS KEY':' >_'.trim();

    if(t==='keyboard'){
      renderKeyboardEditor(box);
    } else if(t==='dictation'){
      renderDictationEditor(box);
    } else if(t==='window'){
      renderWindowEditor(box);
    }
  }
  function renderKeyboardEditor(box){
    const stage=commandStage('keyboard');
    const panel=document.createElement('div'); panel.className='command-options action-stack keyboard-command-panel';
    const copy=document.createElement('span'); copy.className='action-card-copy';
    copy.innerHTML='<strong>'+(recording?'WAITING':keyText(EDIT.keys,'NO KEYS'))+'</strong>'
      +'<em>'+(recording?'PRESS KEYS / ESC CANCEL':'CUSTOM KEYS')+'</em>';
    const rec=document.createElement('button'); rec.type='button'; rec.className='action-record-btn'+(recording?' recording':''); rec.id='recBtn';
    rec.innerHTML='<span>'+(recording?'REC':'REC')+'</span>';
    rec.addEventListener('click',()=>{ if(recording){recording=false;recCb=null;renderValueEditor();} else startRec(keys=>{EDIT.keys=keys;renderValueEditor();preview();}); });
    panel.appendChild(copy); panel.appendChild(rec);
    stage.appendChild(panel); box.appendChild(stage);
  }
  function renderDictationEditor(box){
    const stage=commandStage('dictation');
    const list=document.createElement('div'); list.className='dictation-command-list command-options action-card-list';
    [['hold','PUSH TO TALK'],['toggle','TOGGLE']].forEach(([m,lbl])=>{
      const keys=VOICE_HK[m]||[];
      const row=document.createElement('div'); row.className='dictation-command action-card'+(((EDIT.voice||'hold')===m)?' active':' muted');
      row.tabIndex=0; row.setAttribute('role','button');
      row.innerHTML='<span class="action-card-copy"><strong>'+lbl+'</strong><em>'+keyText(keys,'NO HOTKEY')+'</em></span>';
      const set=document.createElement('button'); set.type='button'; set.className='action-record-btn'; set.textContent='REC';
      set.addEventListener('click',e=>{ e.stopPropagation(); EDIT.voice=m; startRec(ks=>{
        bridge.setVoiceHotkey(m, JSON.stringify(ks), res=>{ let r={}; try{ r=JSON.parse(res); }catch(e){}
          if(r && r.ok===false){ toast('ERROR: '+(r.error||'failed')); return; }
          bridge.getVoiceConfig(vj=>{ try{ VOICE_HK=JSON.parse(vj); }catch(e){} renderValueEditor(); preview(); toast(lbl+' HOTKEY SET'); });
        });
      }, true); });
      bindPseudoButton(row,()=>{ EDIT.voice=m; if(m==='hold') selectedTrigger='press'; renderInspector(); preview(); });
      row.appendChild(set); list.appendChild(row);
    });
    stage.appendChild(list); box.appendChild(stage);
  }
  function renderWindowEditor(box){
    const stage=commandStage('window');
    const list=document.createElement('div'); list.className='window-command-list command-options action-card-list';
    [['forward','WINDOW FORWARD'],['reverse','WINDOW REVERSE']].forEach(([dir,lbl])=>{
      const row=document.createElement('button');
      row.className='window-command action-card'+(((EDIT.direction||'forward')===dir)?' active':' muted');
      row.innerHTML='<span class="action-card-copy"><strong>'+lbl+'</strong></span>';
      row.addEventListener('click',()=>{ EDIT.direction=dir; renderValueEditor(); preview(); });
      list.appendChild(row);
    });
    stage.appendChild(list); box.appendChild(stage);
  }
  function renderBehaviorEditor(target){
    const box=target ? (typeof target==='string' ? $(target) : target) : $('#behaviorEditor'); if(!box) return;
    const summary=$('#behaviorSummaryValue');
    if(EDIT.doubleMs == null) EDIT.doubleMs = DEFAULT_DOUBLE_MS;
    if(EDIT.holdMs == null) EDIT.holdMs = DEFAULT_HOLD_MS;
    const holdTimingEnabled=true;
    const holdDisabled='';
    const holdClass='';
    if(summary) summary.textContent = 'GESTURE TIMING';
    box.innerHTML='';
    box.innerHTML='<div class="gesture-timing">'
      +'<label class="gesture-cell gesture-double"><span>DOUBLE TAP</span><input class="timing-input" id="doubleMs" type="number" min="80" max="900" step="10" value="'+Number(EDIT.doubleMs||DEFAULT_DOUBLE_MS)+'"><b class="unit">MS</b></label>'
      +'<label class="gesture-cell gesture-hold'+holdClass+'"><span>HOLD AFTER</span><input class="timing-input hold-input" id="holdMs" type="number" min="100" max="2000" step="50" value="'+Number(EDIT.holdMs||DEFAULT_HOLD_MS)+'"'+holdDisabled+'><b class="unit">MS</b></label>'
      +'<div class="gesture-cell gesture-release'+holdClass+'"><span>HOLD ACTION</span><button class="timing-switch'+(EDIT.holdOnRelease?' on-release':'')+'" id="holdRelease" type="button" aria-label="Hold action timing"'+holdDisabled+'>'
      +'<span class="timing-option at-threshold">AFTER</span><span class="switch-track"><span class="switch-thumb"></span></span><span class="timing-option on-release-option">RELEASE</span></button></div>'
      +'</div>';
    $('#doubleMs').addEventListener('input',()=>{ EDIT.doubleMs=Number($('#doubleMs').value||DEFAULT_DOUBLE_MS); preview(); });
    $('#holdMs').addEventListener('input',()=>{ EDIT.holdMs=Number($('#holdMs').value||DEFAULT_HOLD_MS); preview(); });
    $('#holdRelease').addEventListener('click',()=>{ EDIT.holdOnRelease=!EDIT.holdOnRelease; renderBehaviorEditor(target); preview(); });
  }
  function setOutput(val){
    const cv=$('#commandValue'); if(cv) cv.textContent=val || '-';
  }
  function preview(){ setOutput(editLabel(EDIT)); renderAssignmentRows(); updateActionButtons(); }
  function composedBindingForEdit(){
    const action=editToAction(EDIT);
    if(isPushToTalkAction(action)) return {voice:true};
    if(EDIT.type==='dictation' && EDIT.voice==='hold') return {voice:true};
    const next=splitBinding(effectiveRaw(selected));
    applyTuneTimingToBinding(next);
    if(!action) return bindingToRaw(next);
    if(selectedTrigger==='hold'){
      next.hold={after_ms:Number(EDIT.holdMs||DEFAULT_HOLD_MS), on_release:!!EDIT.holdOnRelease, action};
    } else {
      next[selectedTrigger]=action;
    }
    return bindingToRaw(next);
  }
  // CLEAR semantics + APPLY lights only when the edit differs from what's applied
  function updateActionButtons(){
    const d=buttonMap[selected]; if(!d) return;
    const clr=$('#clearButton'), ap=$('#applyButton'), rv=$('#revertButton');
    if(rv) rv.disabled=false;
    if(d.fixed){
      const atDefault=Math.abs(Number(MOUSE_CFG.acceleration||1)-1) < 0.001;
      clr.textContent='RESET';
      clr.disabled=atDefault;
      clr.title='Restore pointer acceleration to 1.0x';
      if(rv){ rv.disabled=true; rv.title='Pointer changes are applied live'; }
      ap.disabled=true;
      ap.textContent='SAVED';
      ap.className='action-btn apply saved';
      ap.style.removeProperty('background-color');
      ap.style.removeProperty('border-color');
      ap.style.removeProperty('color');
      return;
    }
    if(rv) rv.title='Revert unsaved edits';
    if(inspectorTab==='tune'){
      clr.textContent='RESET';
      clr.disabled=false;
      clr.title='Restore haptic click and gesture timing defaults';
    } else if(isBaseLayer(editTarget)){ clr.textContent='UNBIND'; clr.disabled = ownRaw(editTarget,selected)===undefined; clr.title='Remove this base binding (button does nothing)'; }
    else if(isOverride(selected)){ clr.textContent='RESET'; clr.disabled=false; clr.title='Remove this override; inherit from base'; }
    else { clr.textContent='RESET'; clr.disabled=true; clr.title='Inherited from base - nothing to reset'; }
    const dirty = actionDirty() || hapticsDirty();
    ap.disabled=!dirty;
    ap.textContent=dirty?'APPLY':'SAVED';
    ap.classList.toggle('saved',!dirty);
    ap.classList.toggle('dirty',dirty);
    ap.style.removeProperty('background-color');
    ap.style.removeProperty('border-color');
    ap.style.removeProperty('color');
  }
  function startRec(cb, leftMods){ recCb=cb; recLeftMods=!!leftMods; recording=true; renderValueEditor(); toast('PRESS A KEY / CHORD · ESC TO CANCEL'); }
  function setType(id){
    const timing={doubleMs:Number(EDIT.doubleMs||DEFAULT_DOUBLE_MS), holdMs:Number(EDIT.holdMs||DEFAULT_HOLD_MS), holdOnRelease:!!EDIT.holdOnRelease};
    EDIT={type:id};
    if(id==='keyboard') EDIT.keys=[];
    if(id==='dictation'){ EDIT.voice='hold'; selectedTrigger='press'; }
    if(id==='window') EDIT.direction='forward';
    Object.assign(EDIT,timing);
    recording=false;
    renderInspector();
  }

  function selectButton(btn){ const d=buttonMap[btn]; if(!d) return;
    selected=btn; recording=false;
    if(!hotspotLayout[view] || !hotspotLayout[view][btn]){ if(!d.fixed){ view=d.view||'3d'; } }
    EDIT = editForTrigger(effectiveRaw(btn), selectedTrigger);
    resetHapticEdit();
    renderView(); renderCallout(); renderInspector();
  }

  function renderAll(){ renderHeaderProfile(); renderProfiles(); renderMatchEditor(); renderView(); renderCallout(); renderInspector(); }

  // ---- data loading ----
  function loadTarget(name){ editTarget=name; selected=selected||'LEFT_SL';
    if(!buttonMap[selected]) selected='LEFT_SL';
    EDIT = editForTrigger(effectiveRaw(selected), selectedTrigger);
    resetHapticEdit();
    renderAll(); }

  function refresh(cb){
    bridge.getProfiles(j=>{
      let names=JSON.parse(j); let pending=names.length; const det={};
      const finish=()=>{
        DETAILS=det;
        DEFAULT_KEY = names.find(n=>det[n] && det[n].isDefault) || 'default';
        order = [DEFAULT_KEY, ...names.filter(n=>n!==DEFAULT_KEY)];
        if(!DETAILS[editTarget]) editTarget=DEFAULT_KEY;
        bridge.getVoiceConfig(vj=>{ try{ VOICE_HK=JSON.parse(vj); }catch(e){}
          const done=()=>{ if(cb)cb(); };
          const loadHaptics=()=>{
            if(bridge.getHapticsConfig) bridge.getHapticsConfig(hj=>{ try{ HAPTICS=normalizeHaptics(JSON.parse(hj)); }catch(e){} done(); });
            else done();
          };
          if(bridge.getMouseConfig) bridge.getMouseConfig(mj=>{ try{ MOUSE_CFG=JSON.parse(mj); }catch(e){} loadHaptics(); });
          else loadHaptics();
        });
      };
      if(!pending){ order=[]; finish(); return; }
      names.forEach(n=>bridge.getProfileDetail(n, dj=>{ det[n]=JSON.parse(dj); if(--pending===0) finish(); }));
    });
  }
  // Run a bridge mutation; on success refresh DETAILS, let onOk adjust the edit
  // target, then re-render. On failure surface the error, leave state untouched.
  function bridgeCall(method, args, onOk){
    if(!bridge[method]){ toast('ERROR: '+method+' unavailable'); return; }
    bridge[method](...args, res=>{
      let r={}; try{ r=JSON.parse(res); }catch(e){}
      if(r && r.ok===false){ toast('ERROR: '+(r.error||'failed')); return; }
      if(onOk) onOk();
    });
  }
  function call(method, args, onOk){
    bridgeCall(method,args,()=>{
      refresh(()=>{ if(onOk) onOk(); loadTarget(editTarget); });
    });
  }

  // ---- apply / clear / revert ----
  function applyEdit(){
    if(buttonMap[selected] && buttonMap[selected].fixed) return;
    const saveAction=actionDirty();
    const saveHaptics=hapticsDirty();
    if(!saveAction && !saveHaptics) return toast('NOTHING TO APPLY');
    const binding=composedBindingForEdit();
    const finish=()=>refresh(()=>{ toast('APPLIED · '+layerDisplay(editTarget)); loadTarget(editTarget); });
    const saveHapticDraft=()=>{
      if(!saveHaptics) return finish();
      bridgeCall('setHapticsConfig',[JSON.stringify(hapticDraftConfig())],finish);
    };
    if(saveAction && binding) bridgeCall('setBinding',[editTarget, selected, JSON.stringify(binding)], saveHapticDraft);
    else saveHapticDraft();
  }
  function clearEdit(){
    if(buttonMap[selected] && buttonMap[selected].fixed){
      MOUSE_CFG.acceleration=1.0;
      const finish=()=>{ renderMouseEditor(); updateActionButtons(); toast('POINTER RESET'); };
      if(bridge.setMouseAcceleration) bridge.setMouseAcceleration(1.0,finish);
      else finish();
      return;
    }
    if(inspectorTab==='tune'){
      HAPTIC_EDIT.clickEnabled=false;
      HAPTIC_EDIT.strength='medium';
      EDIT.doubleMs=DEFAULT_DOUBLE_MS;
      EDIT.holdMs=DEFAULT_HOLD_MS;
      EDIT.holdOnRelease=false;
      renderTuneEditor();
      updateActionButtons();
      toast('TUNE RESET');
      return;
    }
    if(isBaseLayer(editTarget)){
      if(ownRaw(editTarget,selected)===undefined) return;       // nothing bound
      call('clearBinding',[editTarget, selected], ()=>toast('UNBOUND'));
    } else if(isOverride(selected)){
      call('clearBinding',[editTarget, selected], ()=>toast('RESET TO BASE'));
    }
  }
  function revertEdit(){
    if(buttonMap[selected] && buttonMap[selected].fixed) return;
    EDIT=editForTrigger(effectiveRaw(selected), selectedTrigger); resetHapticEdit(); recording=false; renderInspector(); toast('REVERTED');
  }

  // ---- key capture ----
  const TOKEN = {Enter:'enter',Escape:'esc',Tab:'tab',' ':'space',Spacebar:'space',Backspace:'backspace',
    Delete:'delete',Insert:'insert',ArrowUp:'up',ArrowDown:'down',ArrowLeft:'left',ArrowRight:'right',
    Home:'home',End:'end',PageUp:'pageup',PageDown:'pagedown'};
  function evToken(e){ const k=e.key; if(TOKEN[k]) return TOKEN[k];
    if(/^F([1-9]|1[0-2])$/.test(k)) return k.toLowerCase();
    if(k && k.length===1) return k.toLowerCase(); return null; }
  document.addEventListener('keydown',e=>{
    if(recording){
      e.preventDefault();
      if(e.key==='Escape'){ recording=false; recCb=null; renderValueEditor(); toast('CAPTURE CANCELLED'); return; }
      const mods=[], lm=recLeftMods;
      if(e.ctrlKey)mods.push(lm?'lctrl':'ctrl'); if(e.shiftKey)mods.push(lm?'lshift':'shift');
      if(e.altKey)mods.push(lm?'lalt':'alt'); if(e.metaKey)mods.push(lm?'lwin':'win');
      const t=evToken(e); if(t===null || ['ctrl','shift','alt','win'].includes(t)) return; // wait for a real key
      const keys=[...mods,t]; recording=false; const cb=recCb; recCb=null;
      toast('CAPTURED · '+keys.map(keyLabel).join('+'));
      if(cb) cb(keys);
      return;
    }
    if(e.target && /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
    if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'){ e.preventDefault(); applyEdit(); return; }
  });

  // ---- static wiring ----
  $('#applyButton').addEventListener('click',applyEdit);
  $('#clearButton').addEventListener('click',clearEdit);
  $('#revertButton').addEventListener('click',revertEdit);
  $('#newProfile').addEventListener('click',()=>{ creating='profile'; renderMatchEditor(); });
  $('#newOverride').addEventListener('click',()=>{ creating='override'; renderMatchEditor(); });
  $$('.view-btn').forEach(b=>b.addEventListener('click',()=>{ view=b.dataset.view; zoom=1; renderView(); toast('VIEW · '+view.toUpperCase()); }));
  // frameless window controls + drag
  const wc=(sel,fn)=>{ const el=$(sel); if(el) el.addEventListener('click',e=>{ e.stopPropagation(); if(window.bridge)fn(); }); };
  wc('.wc-min',()=>bridge.minimizeWindow&&bridge.minimizeWindow());
  wc('.wc-close',()=>bridge.closeWindow&&bridge.closeWindow());
  const hdr=$('.header');
  if(hdr){ hdr.addEventListener('mousedown',e=>{ if(e.button!==0||e.target.closest('button'))return; if(window.bridge&&bridge.dragWindow)bridge.dragWindow(); }); }

  // ---- boot ----
  function applyAppVersion(){
    const el = $('#appVersion');
    if(!el || !bridge.getAppVersion) return;
    bridge.getAppVersion(v=>{ if(v) el.textContent = String(v); });
  }

  function boot(){
    applyAppVersion();
    bridge.getStatus(s=>applyStatus(JSON.parse(s)));
    if(bridge.statusChanged&&bridge.statusChanged.connect) bridge.statusChanged.connect(s=>applyStatus(JSON.parse(s)));
    refresh(()=>{ editTarget=DEFAULT_KEY; loadTarget(DEFAULT_KEY); });
  }
  if(typeof qt!=='undefined' && qt.webChannelTransport){
    new QWebChannel(qt.webChannelTransport, ch=>{ window.bridge=ch.objects.bridge; boot(); });
  } else {
    window.bridge = makeMock(); boot();
  }

  // ---- mock bridge (browser preview): in-memory config, real-ish behavior ----
  function makeMock(){
    const P = {
      default:{name:'default',displayName:'default',base:null,isBase:true,isOverride:false,isDefault:true,match:null,bindings:{ZL:{voice:true},L:{voice_toggle:true},MINUS:{key:'enter'},
        UP:{key:'up'},DOWN:{key:'down'},LEFT:{key:'left'},RIGHT:{key:'right'},
        LEFT_SL:{hotkey:['alt','tab']},LEFT_SR:{hotkey:['shift','alt','tab']}}},
    };
    const J=x=>JSON.stringify(x);
    const V={hold:['lshift','lctrl','f8'], toggle:['lctrl','lalt','f8']};
    const M={left_stick:'move',right_stick:'scroll',speed:2500,scroll_speed:8,acceleration:2.5};
    const H={click:['ZL','L','LEFT_SL','LEFT_SR','L3','MINUS','CAPTURE','UP','DOWN','LEFT','RIGHT','A','B','X','Y','ZR','R','RIGHT_SL','RIGHT_SR','R3','PLUS','HOME'], strength:'medium'};
    return {
      getAppVersion:cb=>cb('0.0.2'),
      getStatus:cb=>cb(J({running:true,connected:true,battery:3,charging:false,profile:'default'})),
      getVoiceConfig:cb=>cb(J(V)),
      setVoiceHotkey:(m,kj,cb)=>{ V[m]=JSON.parse(kj); cb(J({ok:true})); },
      getMouseConfig:cb=>cb(J(M)),
      getHapticsConfig:cb=>cb(J(H)),
      setHapticsConfig:(hj,cb)=>{ const next=JSON.parse(hj); H.click=[].concat(next.click||[]); H.strength=normalizeStrength(next.strength); cb(J({ok:true})); },
      getStickState:cb=>{ const t=Date.now()/650; cb(J({left:{x:Math.sin(t)*0.55,y:Math.cos(t*0.8)*0.42,magnitude:0.65},right:{x:0,y:0,magnitude:0}})); },
      setMouseAcceleration:(v,cb)=>{ M.acceleration=Number(v); cb(J({ok:true})); },
      getProfiles:cb=>cb(J(Object.keys(P))),
      getProfileDetail:(n,cb)=>cb(J(P[n]?P[n]:{error:'?'})),
      setBinding:(p,b,a,cb)=>{ P[p].bindings[b]=JSON.parse(a); cb(J({ok:true})); },
      clearBinding:(p,b,cb)=>{ delete P[p].bindings[b]; cb(J({ok:true})); },
      setMatch:(p,m,cb)=>{ P[p].match=JSON.parse(m); cb(J({ok:true})); },
      setProfileDisplayName:(p,n,cb)=>{ if(!P[p])return cb(J({ok:false,error:'unknown profile'})); P[p].displayName=String(n||'').trim()||P[p].displayName; cb(J({ok:true})); },
      createBaseProfile:(n,cb)=>{ if(P[n])return cb(J({ok:false,error:'exists'})); P[n]={name:n,displayName:n,base:null,isBase:true,isOverride:false,isDefault:false,match:null,bindings:{}}; cb(J({ok:true})); },
      createOverride:(base,n,m,cb)=>{ const id=base+'::'+n; if(P[id])return cb(J({ok:false,error:'exists'})); P[id]={name:id,displayName:n,base:base,isBase:false,isOverride:true,isDefault:false,match:JSON.parse(m),bindings:{}}; cb(J({ok:true})); },
      createProfile:(n,m,cb)=>{ const id='default::'+n; if(P[id])return cb(J({ok:false,error:'exists'})); P[id]={name:id,displayName:n,base:'default',isBase:false,isOverride:true,isDefault:false,match:JSON.parse(m),bindings:{}}; cb(J({ok:true})); },
      deleteProfile:(n,cb)=>{ if(n==='default')return cb(J({ok:false,error:'cannot delete default'})); delete P[n]; cb(J({ok:true})); },
      pickExe:cb=>cb('demoapp'),
    };
  }
})();
