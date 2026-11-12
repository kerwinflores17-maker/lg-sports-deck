'use strict';
let user=null,anonymousToken=null,catalog=null,myBookings=[],currentPage='home',rankSort='wins',salesPeriod='daily';
let queueSnapshot=[],queueRevision=null,queueSnapshotSession=null;
let testScanStream=null,testScanFrame=null,testScanEpoch=0,testQrTimer=null;
let integrationRevision=null,integrationEpoch=0,integrationDirty={sender:false,recipient:false};
let alertPoll=null,alertEpoch=0,alertSerial=0,lastUnread=null,lastAlertRender=null;
let manualEpoch=0;
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const escapeHTML=x=>String(x??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const money=n=>`₱${Number(n||0).toLocaleString('en-PH')}`;
const sampleDateTime=value=>{const date=new Date(value);return Number.isFinite(date.getTime())?new Intl.DateTimeFormat('en-PH',{timeZone:'Asia/Manila',year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}).format(date)+' (PH time)':'unavailable';};
const timeLabel=n=>{if(n===1440)return '12:00 AM';const hour=Math.floor(n/60),minute=n%60;return `${hour%12||12}:${String(minute).padStart(2,'0')} ${hour<12?'AM':'PM'}`;};
const isPlayer=()=>user?.role==='player';
const isSuper=()=>user?.role==='superuser';
const can=p=>!!user&&(isSuper()||user.permissions.includes(p));
const linkedMatch=()=>{const m=/^#match-([1-9][0-9]{0,8})$/.exec(location.hash);return m?Number(m[1]):null;};
function focusLinkedMatch(id){const target=document.querySelector(`#match-card-${id}`);if(!target){flash('This fictional match is not visible to the signed-in account.',true);return;}target.focus({preventScroll:true});target.scrollIntoView({block:'start'});}
let flashTimer;
function flash(message,error=false){const e=$('#flash');e.textContent=message;e.classList.toggle('error',error);e.hidden=false;clearTimeout(flashTimer);flashTimer=setTimeout(()=>{e.hidden=true;},9000);}
async function api(path,body){
  const opts=body===undefined?{credentials:'same-origin'}:{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':user?.csrf||anonymousToken},body:JSON.stringify(body)};
  const res=await fetch(path,opts);let data={};try{data=await res.json();}catch{}
  if(!res.ok){
    // Reconcile changed/revoked staff access when the server rejects a request.
    // Previously displayed data cannot be remotely erased, but it should not
    // remain in this page's DOM after we learn access was removed.
    if(user&&!isPlayer()&&(res.status===401||res.status===403)&&(path.startsWith('/api/u/')||path.startsWith('/api/staff'))){
      try{
        const identity=await fetch('/api/auth/state',{credentials:'same-origin'}).then(r=>r.json());
        if(!identity.authenticated){await showAnonymous();}
        else {
          const before=[...(user.permissions||[])].sort().join('|'),after=[...(identity.permissions||[])].sort().join('|');
          const changed=before!==after||identity.role!==user.role;
          user=identity;configureUI();
          if(changed){clearSensitiveViews();await refreshCatalog();await navigate(pageAllowed(currentPage)?currentPage:'home');}
        }
      }catch{}
    }
    throw new Error(typeof data.detail==='string'?data.detail:'Please check the fictional test details.');
  }
  return data;
}
function wireForm(selector,fn){$(selector).addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget,button=form.querySelector('button[type=submit]');if(button)button.disabled=true;try{await fn(form);}catch(err){flash(err.message,true);}finally{if(button)button.disabled=false;}});}
const formValues=form=>Object.fromEntries(new FormData(form).entries());
function options(select,items,selected){const previous=selected??select.value;select.replaceChildren();if(!items.length){const o=new Option('No fictional choices available','');select.add(o);return;}for(const item of items)select.add(new Option(item.label,String(item.value)));if([...select.options].some(o=>o.value===String(previous)))select.value=String(previous);}
function playerChecks(holder,players,self,includeSelf=true){holder.replaceChildren();if(!players.length){holder.append(document.createTextNode('No fictional player accounts available.'));return;}for(const p of players){const label=document.createElement('label');label.className='player-check';const box=document.createElement('input');box.type='checkbox';box.value=String(p.id);if(includeSelf&&p.id===self){box.checked=true;box.disabled=true;}const span=document.createElement('span');span.textContent=p.display_name+(includeSelf&&p.id===self?' (you)':'');label.append(box,span);holder.append(label);}}
function selectedPlayers(holder){return [user.id,...[...holder.querySelectorAll('input:checked')].filter(x=>!x.disabled).map(x=>Number(x.value))];}
function clearSensitiveViews(){
  stopTestScanner();clearIssuedTestQr();clearTestIntegrationSettings();stopPlayerAlertPolling();clearManualReview();
  for(const id of ['manual-mine-list','my-bookings','queue-list','match-list','ranking-list','notices-list','player-alerts','roster-list','staff-bookings','sales-list','staff-list','rental-players','open-players','court-players','open-sessions'])$('#'+id).replaceChildren();
  for(const select of $$('#rental-booking,#queue-session,#open-session,#roster-session,.manual-player'))select.replaceChildren();
  myBookings=[];queueSnapshot=[];queueRevision=null;queueSnapshotSession=null;
}
async function showAnonymous(){
  user=null;catalog=null;clearSensitiveViews();for(const form of $$('#portal form'))form.reset();$('#identity').textContent='';
  anonymousToken=(await api('/api/auth/bootstrap')).csrf;$('#auth-shell').hidden=false;$('#portal').hidden=true;
}
function pageAllowed(page){return ({home:true,open:isPlayer()||can('session.view'),court:isPlayer(),bookings:isPlayer(),queue:isPlayer()||can('queue.view')||can('queue.manage')||can('attendance.qr.display'),matches:isPlayer()||can('queue.view'),ranking:isPlayer()||can('rankings.view'),notices:isPlayer(),manual:!!catalog?.fictional_manual_qr_enabled&&['superuser','admin','manager'].includes(user?.role)&&can('payments.manual_verify'),ops:isSuper()||['session.manage','bookings.view','attendance.noshow','settings.rates.edit','settings.limits.edit'].some(can),sales:can('sales.view'),schedule:isSuper()||can('schedule.manage'),access:isSuper(),config:isSuper()})[page]||false;}
function configureUI(){
  $('#auth-shell').hidden=true;$('#portal').hidden=false;$('#identity').textContent=`${user.name} · ${user.role}`;
  for(const button of $$('#navigation [data-page]'))button.hidden=!pageAllowed(button.dataset.page);
  $('.home-grid').hidden=!isPlayer();$('#home-staff-note').hidden=isPlayer();$('#notice-panel').hidden=!isPlayer();$('#open-book-panel').hidden=!isPlayer();$('#checkin-controls').hidden=!isPlayer();$('#assign-controls').hidden=!can('queue.manage');$('#queue-order-help').hidden=!can('queue.manage');$('#qr-issue-panel').hidden=!can('attendance.qr.display');
  $('#session-panel').hidden=!can('session.manage');$('#rates-panel').hidden=!can('settings.rates.edit');$('#limits-panel').hidden=!can('settings.limits.edit');$('#roster-panel').hidden=!(can('bookings.view')||can('attendance.noshow'));$('#staff-bookings-panel').hidden=!can('bookings.view');
  $('#accept-notice').disabled=!!user.notice_acknowledged;$('#notice-status').textContent=user.notice_acknowledged?' Acknowledged by this fictional account.':' Not yet acknowledged by this account.';
}
async function loadIdentity(){const state=await api('/api/auth/state');if(!state.authenticated){await showAnonymous();return false;}user=state;anonymousToken=null;configureUI();startPlayerAlertPolling();await refreshCatalog();
  const match=linkedMatch();if(match&&pageAllowed('matches'))currentPage='matches';if(!pageAllowed(currentPage))currentPage='home';
  await navigate(currentPage);if(match&&currentPage==='matches')focusLinkedMatch(match);return true;}
function fillHours(){for(const select of $$('.hours'))options(select,Array.from({length:18},(_,i)=>({value:i+6,label:timeLabel((i+6)*60)})),6);options($('#court-duration'),Array.from({length:18},(_,i)=>({value:i+1,label:`${i+1} hour${i?'s':''}`})),1);}
async function refreshCatalog(){catalog=await api('/api/u/catalog');$('#sample-retention-status').textContent=!catalog.sample_auto_retention_test_enabled?'Sample-only automatic cleanup is disabled in this test.':catalog.sample_auto_retention_test_status==='failed'?'Sample-only cleanup reported a test failure; operator review is needed.':'Sample-only 7-day cleanup is enabled for new fictional clips while this test app runs; it does not clean backups.';const s=catalog.settings;$('#home-open').textContent=money(s.open_rate);$('#home-court').textContent=money(s.court_rate);$('#court-rate').textContent=money(s.court_rate);$('#court-limit').textContent=s.court_limit;
  if(!$('#court-day').value)$('#court-day').value=catalog.today;$('#court-day').min=catalog.today;if(!$('#session-day').value)$('#session-day').value=catalog.today;$('#session-day').min=catalog.today;
  for(const [name,value] of Object.entries(s)){const field=$(`#rates-form [name="${name}"]`)||$(`#limits-form [name="${name}"]`);if(field)field.value=value;}
  const sessions=catalog.sessions.map(x=>({value:x.id,label:`${x.label} · ${x.day} ${timeLabel(x.start_min)}–${timeLabel(x.end_min)} · ${x.occupied}/${x.capacity}${x.ended?' · ended':''}`}));
  options($('#open-session'),sessions.filter(item=>{const x=catalog.sessions.find(s=>s.id===item.value);return !x.ended&&!x.full&&!x.registered;}));options($('#queue-session'),sessions);options($('#roster-session'),sessions);
  if(isPlayer()){playerChecks($('#open-players'),catalog.players,user.id);playerChecks($('#court-players'),catalog.players,user.id);}renderSessions();
  refreshTodaySchedule().catch(()=>{});
  syncManualTestMode();configureUI();
}
function renderSessions(){const el=$('#open-sessions');if(!catalog.sessions.length){el.innerHTML='<div class="panel empty">No fictional Open Play sessions are configured.</div>';return;}
  el.innerHTML=catalog.sessions.map(s=>`<article class="panel session-card"><span class="pill ${s.full?'full':''}">${s.ended?'ENDED':s.full?'FULL':'SAMPLE SESSION'}</span><h3>${escapeHTML(s.label)}</h3><p>${escapeHTML(s.day)} · ${timeLabel(s.start_min)}–${timeLabel(s.end_min)}</p><p>${s.occupied}/${s.capacity} fictional places · ${s.waiting} waiting</p>${isPlayer()&&!s.ended&&s.full&&!s.registered?`<button class="btn outline" data-wait="${s.id}" ${s.on_waitlist?'disabled':''}>${s.on_waitlist?'On test waitlist':'Join free test waitlist'}</button>`:''}${isPlayer()&&s.registered?'<span class="helper">This account is registered.</span>':''}</article>`).join('');}
async function navigate(page){if(!pageAllowed(page)){flash('This fictional account does not have that page permission.',true);return;}if(page!=='queue'){stopTestScanner();clearIssuedTestQr();}if(page!=='config')clearTestIntegrationSettings();if(page!=='manual')clearManualReview();currentPage=page;for(const section of $$('.page'))section.hidden=section.id!==`page-${page}`;for(const btn of $$('#navigation [data-page]'))btn.classList.toggle('active',btn.dataset.page===page);const nav=$(`#navigation [data-page="${page}"]`);$('#crumb').textContent=nav?.querySelector('span')?.textContent.trim()||'Book & play';
  try{if(page==='bookings')await refreshBookings();if(page==='queue')await refreshQueue();if(page==='matches')await refreshMatches();if(page==='ranking')await refreshRanking();if(page==='notices')await refreshNotices();if(page==='manual')await refreshManualReview();if(page==='ops')await refreshOps();if(page==='sales')await refreshSales();if(page==='schedule')await refreshSchedule();if(page==='access')await refreshStaff();if(page==='config')await refreshTestIntegrationSettings();}catch(err){flash(err.message,true);}}
function bookingMarkup(b){const kind=b.kind==='court'?`Court ${String(b.court_id).padStart(2,'0')}`:'Open Play';const names=b.players.map(p=>`${escapeHTML(p.display_name)}${p.attendance==='no_show'?' (no-show)':''}`).join(', ');return `<div class="row"><div><span class="row-title">${escapeHTML(b.reference)} · ${kind}</span><small>${escapeHTML(b.day)} · ${timeLabel(b.start_min)}–${timeLabel(b.end_min)} · ${names}</small><small>Simulated GCash · no real booking</small></div><b>${money(b.amount)}</b></div>`;}
async function refreshBookings(){const response=await api('/api/u/my-bookings');myBookings=response.bookings;$('#my-bookings').innerHTML=myBookings.length?myBookings.map(bookingMarkup).join(''):'<div class="empty">No fictional bookings for this account.</div>';
  const rentals=myBookings.filter(x=>x.kind==='court');options($('#rental-booking'),rentals.map(x=>({value:x.id,label:`${x.reference} · Court ${x.court_id} · ${x.day}`})));renderRentalPlayers();
  if(catalog?.fictional_manual_qr_enabled)await refreshManualMine();}
function renderRentalPlayers(){const id=Number($('#rental-booking').value),b=myBookings.find(x=>x.id===id);playerChecks($('#rental-players'),b?b.players.map(p=>({id:p.id,display_name:p.display_name})):[],user.id);}
async function refreshQueue(){const sid=Number($('#queue-session').value),holder=$('#queue-list');
  queueSnapshot=[];queueRevision=null;queueSnapshotSession=null;
  if(!sid){holder.innerHTML='<div class="empty">No test session is available. Staff also need session-view access.</div>';optionsForManual([]);return;}
  if(isPlayer()&&!catalog.sessions.find(s=>s.id===sid)?.registered){holder.innerHTML='<div class="empty">This fictional account needs a simulated registration before viewing the queue.</div>';optionsForManual([]);return;}
  if(!isPlayer()&&!can('queue.view')){holder.innerHTML='<div class="empty">Queue-view permission is not granted.</div>';optionsForManual([]);return;}
  const data=await api(`/api/u/queue/${sid}`);if(Number($('#queue-session').value)!==sid)return;
  const rows=data.queue;queueSnapshot=rows;queueRevision=data.revision;queueSnapshotSession=sid;
  holder.innerHTML=rows.length?rows.map((p,i)=>{
    const name=escapeHTML(p.display_name),id=Number(p.id);
    const move=can('queue.manage')?`<div class="actions queue-moves"><button type="button" class="btn outline" data-queue-move="-1" data-queue-id="${id}" aria-label="Move ${name} up" ${i===0?'disabled':''}>↑</button><button type="button" class="btn outline" data-queue-move="1" data-queue-id="${id}" aria-label="Move ${name} down" ${i===rows.length-1?'disabled':''}>↓</button></div>`:'';
    return `<div class="row"><div><span class="row-title">${i+1}. ${name}</span><small>${escapeHTML(p.classification)} · fictional waiting queue</small></div>${move}</div>`;
  }).join(''):'<div class="empty">No fictional players are waiting.</div>';optionsForManual(rows);}
function optionsForManual(rows){const items=rows.map(p=>({value:p.user_id,label:p.display_name}));for(const el of $$('.manual-player'))options(el,items);}
async function refreshMatches(){const data=await api('/api/u/matches');const holder=$('#match-list');if(!data.matches.length){holder.innerHTML='<div class="panel empty">No fictional matches are visible to this account.</div>';return;}
  holder.innerHTML=data.matches.map(m=>{const team=t=>m.players.filter(p=>p.team===t).map(p=>escapeHTML(p.display_name)).join(', ');const rec=m.can_control?`<p class="helper">Sample recording: ${escapeHTML(m.recording_state||'not started')}${m.sample_expires_at?` · test expiry ${escapeHTML(sampleDateTime(m.sample_expires_at))}`:''}</p>`:'';const controls=[];
    if(m.can_end)controls.push(`<button class="btn outline" data-match="${m.id}" data-match-action="end">End test match</button>`);
    if(m.can_control&&m.status==='active'&&(!m.recording_state||m.recording_state==='recording'))controls.push(`<button class="btn outline" data-match="${m.id}" data-match-action="recording/start">Start sample recording</button>`);
    if(m.can_control&&m.recording_state==='recording')controls.push(`<button class="btn outline" data-match="${m.id}" data-match-action="recording/stop">Stop sample recording</button>`);
    if(m.can_confirm)controls.push(`<button class="btn outline" data-match="${m.id}" data-match-action="confirm">Confirm for my team</button>`);
    const score=m.can_score?`<form class="score-form" data-match-score="${m.id}"><label>Team A<input type="number" name="score_a" min="0" max="99" required></label><label>Team B<input type="number" name="score_b" min="0" max="99" required></label><button type="submit" class="btn outline">Enter test score</button></form>`:'';
    return `<article class="panel match-card" id="match-card-${Number(m.id)}" tabindex="-1"><div class="match-head"><h2>Match ${m.id} · Court ${m.court_id}</h2><span class="pill">${escapeHTML(m.service==='openplay'?'OPEN PLAY':'RENTAL')}</span></div><p class="helper">${escapeHTML(m.status)} · score ${escapeHTML(m.score_state)}${m.score_a!==null?` · ${m.score_a}–${m.score_b}`:''}</p><div class="team-grid"><div class="team"><b>Team A</b><span>${team('A')}</span></div><div class="team"><b>Team B</b><span>${team('B')}</span></div></div>${rec}<div class="actions gap">${controls.join('')}</div>${score}${m.can_watch?`<video controls preload="metadata" src="/api/u/matches/${m.id}/replay">Sample stock replay (not your match)</video>`:''}</article>`;}).join('');}
async function refreshRanking(){const data=await api('/api/u/ranking');const sorted=[...data.ranking].sort((a,b)=>b[rankSort]-a[rankSort]||a.display_name.localeCompare(b.display_name));$('#ranking-list').innerHTML=sorted.length?sorted.map((r,i)=>`<div class="row"><div><b>${i+1}. ${escapeHTML(r.display_name)}</b><small>${r.wins} wins · ${r.losses} losses</small></div><b>${r.win_rate}%</b></div>`).join(''):'<div class="empty">No confirmed fictional Open Play results.</div>';}
function stopPlayerAlertPolling(){
  if(alertPoll!==null)clearInterval(alertPoll);alertPoll=null;alertEpoch++;alertSerial++;lastUnread=null;lastAlertRender=null;
  const badge=$('#notification-count');badge.hidden=true;badge.textContent='0';
}
function startPlayerAlertPolling(){
  stopPlayerAlertPolling();if(!isPlayer())return;
  refreshPlayerAlerts(true).catch(()=>{});
  alertPoll=setInterval(()=>{if(!document.hidden&&isPlayer())refreshPlayerAlerts(true).catch(()=>{});},15000);
}
function renderPlayerAlerts(alerts){
  const holder=$('#player-alerts'),signature=JSON.stringify(alerts);
  if(signature===lastAlertRender)return;lastAlertRender=signature;
  if(!alerts.length){holder.innerHTML='<div class="empty">No fictional queue, ranking or replay updates yet.</div>';return;}
  holder.innerHTML=alerts.map(n=>{
    const id=Number(n.id),match=Number(n.match_id),kind=n.kind;
    if(!Number.isSafeInteger(id)||id<1||!Number.isSafeInteger(match)||match<1||!['queue_assignment','video_ready','ranking_update'].includes(kind))return '';
    const label=kind==='queue_assignment'?'QUEUE ASSIGNMENT':kind==='video_ready'?'SAMPLE REPLAY':'RANKING UPDATE';
    const action=kind==='ranking_update'?'ranking':'match',labelAction=action==='ranking'?'View test rankings':'Open test match';
    const read=n.read_at!==null;
    return `<article class="alert-card ${read?'':'unread'}"><div class="alert-kind">${label} · ${read?'READ':'NEW'}</div><h3>${escapeHTML(n.title)}</h3><p>${escapeHTML(n.body)}</p><div class="actions"><button type="button" class="btn outline" data-alert-id="${id}" data-alert-match="${match}" data-alert-action="${action}">${labelAction}</button>${read?'':`<button type="button" class="btn outline" data-alert-id="${id}" data-alert-action="read">Mark as read</button>`}</div></article>`;
  }).join('');
}
async function refreshPlayerAlerts(quiet=false){
  if(!isPlayer())return;const epoch=alertEpoch,serial=++alertSerial,uid=user.id;
  const data=await api('/api/u/player-alerts');
  if(epoch!==alertEpoch||serial!==alertSerial||!isPlayer()||user.id!==uid)return;
  if(data.mode!=='fictional-only'||!Array.isArray(data.alerts)||!Number.isSafeInteger(data.unread_count)||data.unread_count<0)throw new Error('Invalid fictional notification response.');
  const badge=$('#notification-count');badge.hidden=data.unread_count===0;badge.textContent=data.unread_count>99?'99+':String(data.unread_count);
  badge.setAttribute('aria-label',`${data.unread_count} unread fictional notification${data.unread_count===1?'':'s'}`);
  if(quiet&&lastUnread!==null&&data.unread_count>lastUnread&&currentPage!=='notices')flash('New fictional notification. Open Notifications to view it.');
  lastUnread=data.unread_count;if(currentPage==='notices')renderPlayerAlerts(data.alerts);
}
async function refreshNotices(){
  const epoch=alertEpoch,uid=user?.id;const [data]=await Promise.all([api('/api/u/notices'),refreshPlayerAlerts()]);
  if(epoch!==alertEpoch||!isPlayer()||user.id!==uid||currentPage!=='notices')return;
  const lines=[...(data.test_mail||[]),...(data.extra_test_mail||[])].sort((a,b)=>b.created_at-a.created_at);
  $('#notices-list').innerHTML=lines.length?lines.map(n=>`<div class="row"><div><b>${escapeHTML(n.subject)}</b><small>${escapeHTML(n.body)}</small><small>Test state: ${escapeHTML(n.state)} · no email sent</small>${['replay','rental_replay','queue_assignment'].includes(n.kind)&&Number(n.match_id)>0?`<button type="button" class="btn outline gap" data-open-test-match="${Number(n.match_id)}">Open fictional match</button>`:''}</div></div>`).join(''):'<div class="empty">No fictional email jobs.</div>';
}
async function refreshOps(){if(can('bookings.view')){const data=await api('/api/u/ops/bookings');$('#staff-bookings').innerHTML=data.bookings.length?data.bookings.map(bookingMarkup).join(''):'<div class="empty">No fictional bookings.</div>';}$('#roster-list').replaceChildren();}
async function refreshRoster(){const sid=Number($('#roster-session').value);if(!sid){$('#roster-list').innerHTML='<div class="empty">No visible test sessions. Session-view permission may be required.</div>';return;}const data=await api(`/api/u/ops/roster/${sid}`);$('#roster-list').innerHTML=data.players.length?data.players.map(p=>`<div class="row"><div><b>${escapeHTML(p.display_name)}</b><small>${escapeHTML(p.attendance)}${p.checked_in?' · checked in':''}</small></div>${can('attendance.noshow')&&p.attendance==='booked'&&!p.checked_in?`<button class="btn outline" data-noshow="${p.id}">Mark no-show</button>`:''}</div>`).join(''):'<div class="empty">No fictional players in this session.</div>';}
async function refreshSales(){const data=await api(`/api/u/ops/sales?period=${salesPeriod}`);$('#sales-list').innerHTML=`<div class="detail"><span>Simulated total · ${escapeHTML(data.date)}</span><b>${money(data.total)}</b></div><div class="detail"><span>Simulated transactions</span><b>${data.count}</b></div>${data.breakdown.map(r=>`<div class="detail"><span>${escapeHTML(r.kind==='court'?'Court rental':r.kind==='openplay'?'Open Play':r.kind)} · ${escapeHTML(r.payment_method==='simulated_gcash'?'simulated GCash':r.payment_method)}</span><b>${money(r.total)}</b></div>`).join('')}<p class="helper">No actual provider fees or sales; not a BIR invoice.</p>`;}
let calMonth=new Date(); let calSelected=null;
function isoDate(d){ return d.toISOString().slice(0,10); }
async function refreshTodaySchedule(){ try{ if(!catalog?.today) return; const day=catalog.today; const data=await api(`/api/u/schedule/day?day=${day}`); const h=document.getElementById('today-schedule'); if(!h) return; h.innerHTML=data.slots.map(s=>`<div class="detail"><span>${s.start_hour}:00–${s.start_hour+s.duration}:00</span><b>${escapeHTML(s.label)}</b><span class="pill ${s.kind==='open_play'?'':'full'}" style="font-size:11px">${escapeHTML(s.kind==='open_play'?'OPEN PLAY':'RENTAL')}</span></div>`).join(''); }catch{} }
async function computeDayCounts(dayIso, slots, bookings, sessions){
  // bookings: array of {day, start_min, end_min, kind, amount, players?}
  // sessions: catalog.sessions with day, start_min, end_min, capacity, occupied
  const counts = slots.map(s=>({slot:s, open_occupied:0, rental_courts:0}));
  // Open Play occupancy: sum occupied for sessions that overlap slot on this day
  for(const sess of (sessions||[])){
    if(sess.day!==dayIso) continue;
    for(const c of counts){
      if(c.slot.kind!=='open_play') continue;
      if(sess.start_min < c.slot.end_min && sess.end_min > c.slot.start_min){
        c.open_occupied += Number(sess.occupied||0);
        c.open_capacity = sess.capacity || 22;
      }
    }
  }
  // Rental courts: count bookings that overlap slot
  for(const b of (bookings||[])){
    if(b.day!==dayIso) continue;
    if(b.kind!=='court') continue;
    for(const c of counts){
      if(c.slot.kind!=='rental') continue;
      if(b.start_min < c.slot.end_min && b.end_min > c.slot.start_min){
        c.rental_courts += 1;
      }
    }
  }
  return counts;
}
async function renderCalendar(){
  try{
    const templateData=await api('/api/u/schedule/template');
    const holder=document.getElementById('schedule-template');
    if(holder) holder.innerHTML=templateData.slots.map(s=>`<div class="detail"><span style="min-width:90px;display:inline-block">${s.start_hour}:00-${s.start_hour+s.duration}:00</span><span style="flex:1">${escapeHTML(s.label)}</span><span class="pill ${s.kind==='open_play'?'':'full'}" style="font-size:11px">${escapeHTML(s.kind==='open_play'?'OPEN PLAY':'RENTAL')}</span></div>`).join('');
    if(isSuper()) renderTemplateEditor(templateData.slots);
    // fetch bookings/sessions for counts
    let allBookings=[]; let sessions=catalog?.sessions||[];
    try{
      if(isSuper() || can('bookings.view')){ const b=await api('/api/u/ops/bookings'); allBookings=b.bookings; }
      else if(isPlayer()){ const b=await api('/api/u/my-bookings'); allBookings=b.bookings; }
    }catch{}
    // determine month to show
    if(!calSelected) calSelected = catalog?.today || isoDate(new Date());
    if(!calMonth || isNaN(calMonth.getTime())) calMonth=new Date(calSelected);
    const year=calMonth.getFullYear(), month=calMonth.getMonth();
    const monthLabel=document.getElementById('cal-month-label');
    if(monthLabel) monthLabel.textContent=calMonth.toLocaleDateString('en-US',{month:'long',year:'numeric'});
    const first=new Date(year,month,1), last=new Date(year,month+1,0);
    const startDow=first.getDay();
    const grid=document.getElementById('cal-grid');
    if(!grid) return;
    grid.innerHTML='';
    for(let i=0;i<startDow;i++){ const ph=document.createElement('div'); ph.className='day'; ph.style.visibility='hidden'; grid.appendChild(ph); }
    for(let d=1; d<=last.getDate(); d++){
      const date=new Date(year,month,d);
      const id=isoDate(date);
      const el=document.createElement('div'); el.className='day';
      if(id===calSelected) el.classList.add('selected');
      if(id===catalog?.today) el.classList.add('today');
      // get slots for this day (with override)
      let daySlots=[];
      try{ const dd=await api(`/api/u/schedule/day?day=${id}`); daySlots=dd.slots; }catch{ daySlots=templateData.slots.map(s=>({...s, start_min:s.start_hour*60, end_min:(s.start_hour+s.duration)*60})); }
      const bars=daySlots.map(s=>`<div class="bar ${s.kind==='open_play'?'open':'rental'}" title="${escapeHTML(s.label)}"></div>`).join('');
      // compute counts for badge
      const counts=await computeDayCounts(id, daySlots.map(s=>({start_min:s.start_hour*60, end_min:(s.start_hour+s.duration)*60, kind:s.kind, label:s.label, start_hour:s.start_hour, duration:s.duration})), allBookings, sessions);
      // build badge text: for each day, show summary like "1/22" for open play with most occupancy, or "1/2" for rental
      let badgeText='';
      const openCounts=counts.filter(c=>c.slot.kind==='open_play');
      const rentalCounts=counts.filter(c=>c.slot.kind==='rental');
      if(openCounts.length){
        const maxOpen=Math.max(...openCounts.map(c=>c.open_occupied));
        const cap=openCounts[0]?.open_capacity||22;
        badgeText=`Open Play ${maxOpen}/${cap}`;
        if(openCounts.some(c=>c.open_occupied>=cap)) badgeText+=' FULL';
      }
      if(rentalCounts.length){
        const totalRentalCourts=rentalCounts.reduce((a,c)=>a+c.rental_courts,0);
        const maxCourts=Math.max(...rentalCounts.map(c=>c.rental_courts));
        badgeText += (badgeText?' · ':'') + `Rental ${maxCourts}/2`;
      }
      if(!badgeText) badgeText='No bookings';
      el.innerHTML=`<div class="daynum">${d}</div><div class="bars">${bars}</div><div class="badges"><span class="badge">${escapeHTML(badgeText)}</span></div>`;
      el.onclick=()=>{ calSelected=id; renderCalendar(); renderCalDetail(id, daySlots); };
      grid.appendChild(el);
    }
    // also update detail for selected
    renderCalDetail(calSelected, null);
    await refreshTodaySchedule();
  }catch(e){ const h=document.getElementById('schedule-template'); if(h) h.innerHTML=`<p class="helper">Schedule unavailable: ${escapeHTML(e.message)}</p>`; }
}
async function renderCalDetail(dayIso, preSlots){
  try{
    const day=dayIso||calSelected||catalog.today;
    const detailDate=document.getElementById('cal-detail-date');
    if(detailDate){ const d=new Date(day); detailDate.textContent=d.toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}) + (day===catalog?.today?' — Today':''); }
    let slots=preSlots;
    if(!slots){ const d=await api(`/api/u/schedule/day?day=${day}`); slots=d.slots; }
    const counts=await (async()=>{
      let bookings=[]; let sessions=catalog?.sessions||[];
      try{ if(isSuper()||can('bookings.view')){ const b=await api('/api/u/ops/bookings'); bookings=b.bookings; } else if(isPlayer()){ const b=await api('/api/u/my-bookings'); bookings=b.bookings; } }catch{}
      const enriched=slots.map(s=>({start_min:s.start_hour*60, end_min:(s.start_hour+s.duration)*60, kind:s.kind, label:s.label, start_hour:s.start_hour, duration:s.duration}));
      return computeDayCounts(day, enriched, bookings, sessions);
    })();
    const holder=document.getElementById('cal-day-slots');
    if(holder){
      holder.innerHTML=counts.map(c=>{
        const s=c.slot;
        const label = s.kind==='open_play' ? `${s.label} — ${c.open_occupied}/${c.open_capacity||22}${c.open_occupied>=(c.open_capacity||22)?' FULL':''}` : `${s.label} — ${c.rental_courts}/2 courts reserved`;
        return `<div class="detail"><span>${s.start_hour}:00–${s.start_hour+s.duration}:00</span><b>${escapeHTML(label)}</b><span class="pill ${s.kind==='open_play'?'op':'rb'}" style="font-size:11px">${escapeHTML(s.kind==='open_play'?'OPEN PLAY':'RENTAL')}</span></div>`;
      }).join('');
    }
    // bookings list for day
    try{
      let bookings=[];
      if(isSuper()||can('bookings.view')){ const b=await api('/api/u/ops/bookings'); bookings=b.bookings.filter(x=>x.day===day); }
      else if(isPlayer()){ const b=await api('/api/u/my-bookings'); bookings=b.bookings.filter(x=>x.day===day); }
      const bh=document.getElementById('cal-day-bookings');
      if(bh){
        if(!bookings.length) bh.innerHTML='<p class="helper">No bookings for this day yet.</p>';
        else bh.innerHTML=bookings.map(b=>`<div class="detail"><span>${b.start_min!==undefined?Math.floor(b.start_min/60)+':00':''}–${b.end_min!==undefined?Math.floor(b.end_min/60)+':00':''} · ${escapeHTML(b.kind==='openplay'?'Open Play':'Court '+(b.court_id||''))}</span><b>${money(b.amount)}</b><span>${escapeHTML(b.state)}</span></div>`).join('');
      }
    }catch{}
  }catch(e){ const h=document.getElementById('cal-day-slots'); if(h) h.innerHTML=`<p class="helper">Detail unavailable: ${escapeHTML(e.message)}</p>`; }
}
async function refreshSchedule(){ await renderCalendar(); }

async function refreshScheduleSales(){try{const by=await api(`/api/u/ops/sales/by-schedule?period=${salesPeriod}`);const holder=document.getElementById('sales-by-schedule');if(!holder)return;const best=by.by_schedule.reduce((m,x)=>x.revenue>m.revenue?x:m,by.by_schedule[0]||{revenue:0});holder.innerHTML=by.by_schedule.map(r=>`<div class="detail" style="${r.revenue===best.revenue&&r.revenue>0?'background:#fff7cc':''}"><span>${escapeHTML(r.label)} \u00b7 ${escapeHTML(r.kind==='open_play'?'Open Play':'Rental')} ${r.start_hour}:00-${r.start_hour+r.duration}:00 \u00b7 ${r.bookings} bookings</span><b>${money(r.revenue)}${r.revenue===best.revenue&&r.revenue>0?' \u2605 best':''}</b></div>`).join('')||'<div class="empty">No fictional bookings in this period.</div>';const lab=document.getElementById('sales-by-schedule-period');if(lab)lab.textContent=`Per-schedule \u00b7 ${by.period} \u00b7 ${by.day}`;}catch(e){const h=document.getElementById('sales-by-schedule');if(h)h.innerHTML=`<p class="helper">Per-schedule report unavailable: ${escapeHTML(e.message)}</p>`;}}
const _origRefreshSales = refreshSales;
async function refreshSales(){const data=await api(`/api/u/ops/sales?period=${salesPeriod}`);document.getElementById('sales-list').innerHTML=`<div class="detail"><span>Simulated total \u00b7 ${escapeHTML(data.date)}</span><b>${money(data.total)}</b></div><div class="detail"><span>Simulated transactions</span><b>${data.count}</b></div>${data.breakdown.map(r=>`<div class="detail"><span>${escapeHTML(r.kind==='court'?'Court rental':r.kind==='openplay'?'Open Play':r.kind)} \u00b7 ${escapeHTML(r.payment_method==='simulated_gcash'?'simulated GCash':r.payment_method)}</span><b>${money(r.total)}</b></div>`).join('')}<p class="helper">No actual provider fees or sales; not a BIR invoice.</p>`;await refreshScheduleSales();}
// --- DAILY SCHEDULE VISUAL EDITOR (repeating + per-date override) ---
let templateEditorSlots=null, overrideEditorSlots=null;
function computeStartHours(slots){let cur=6;return slots.map(s=>{const start=cur; cur+=Number(s.duration)||0; return {...s, _start:start};});}
function renderTemplateEditor(slots){
  templateEditorSlots = slots.map(s=>({kind:s.kind, duration:s.duration, label:s.label}));
  const holder=document.getElementById('schedule-editor'); if(!holder) return;
  const withStart=computeStartHours(templateEditorSlots);
  holder.innerHTML=withStart.map((s,i)=>`<div class="detail" data-idx="${i}" style="gap:8px;align-items:center"><span style="min-width:80px">${s._start}:00–${s._start+s.duration}:00</span><select data-field="kind" data-idx="${i}" style="width:120px"><option value="rental" ${s.kind==='rental'?'selected':''}>Rental</option><option value="open_play" ${s.kind==='open_play'?'selected':''}>Open Play</option></select><input data-field="duration" data-idx="${i}" type="number" min="1" max="12" value="${s.duration}" style="width:65px"><input data-field="label" data-idx="${i}" value="${escapeHTML(s.label)}" maxlength="80" style="flex:1"><button type="button" class="btn outline" data-del-tmpl="${i}" style="padding:4px 8px">✕</button></div>`).join('');
  const total=templateEditorSlots.reduce((a,b)=>a+Number(b.duration||0),0);
  const status=document.getElementById('schedule-editor-status');
  if(status) status.textContent=`Total: ${total}h (need 18h = 6 AM–12 MN). ${total!==18?'— hindi pa ma-save, ayusin yung durations.':''}`;
}
function renderOverrideEditor(slots){
  overrideEditorSlots = slots ? slots.map(s=>({kind:s.kind, duration:s.duration, label:s.label})) : null;
  const holder=document.getElementById('schedule-override-editor'); if(!holder) return;
  if(!slots){ holder.innerHTML='<p class="helper">No override loaded — repeating template ang gamit sa araw na ito.</p>'; return; }
  const withStart=computeStartHours(overrideEditorSlots);
  holder.innerHTML=withStart.map((s,i)=>`<div class="detail" data-oidx="${i}" style="gap:8px;align-items:center"><span style="min-width:80px">${s._start}:00–${s._start+s.duration}:00</span><select data-ofield="kind" data-oidx="${i}" style="width:120px"><option value="rental" ${s.kind==='rental'?'selected':''}>Rental</option><option value="open_play" ${s.kind==='open_play'?'selected':''}>Open Play</option></select><input data-ofield="duration" data-oidx="${i}" type="number" min="1" max="12" value="${s.duration}" style="width:65px"><input data-ofield="label" data-oidx="${i}" value="${escapeHTML(s.label)}" maxlength="80" style="flex:1"><button type="button" class="btn outline" data-del-ov="${i}" style="padding:4px 8px">✕</button></div>`).join('') + `<div class="actions gap"><button type="button" class="btn outline" id="add-override-slot">+ Add slot to override</button></div>`;
  const total=overrideEditorSlots.reduce((a,b)=>a+Number(b.duration||0),0);
  const st=document.getElementById('schedule-override-status');
  if(st) st.textContent=`Override total: ${total}h (need 18h).`;
}
async function loadTemplateIntoEditor(){ try{ const data=await api('/api/u/schedule/template'); renderTemplateEditor(data.slots); }catch(e){ const s=document.getElementById('schedule-editor-status'); if(s) s.textContent='Failed to load: '+e.message; } }
document.getElementById('schedule-editor')?.addEventListener('input',e=>{
  const idx=e.target.dataset.idx; if(idx===undefined) return;
  const field=e.target.dataset.field; if(!field) return;
  if(field==='duration') templateEditorSlots[Number(idx)].duration=Math.max(1,Math.min(12,Number(e.target.value)||1));
  else if(field==='kind') templateEditorSlots[Number(idx)].kind=e.target.value;
  else if(field==='label') templateEditorSlots[Number(idx)].label=e.target.value;
  const total=templateEditorSlots.reduce((a,b)=>a+Number(b.duration||0),0);
  const st=document.getElementById('schedule-editor-status'); if(st) st.textContent=`Total: ${total}h (need 18h). ${total!==18?'— hindi pa ma-save.':''}`;
});
document.getElementById('schedule-editor')?.addEventListener('click',e=>{
  const del=e.target.dataset.delTmpl; if(del===undefined) return;
  templateEditorSlots.splice(Number(del),1); renderTemplateEditor(computeStartHours(templateEditorSlots).map(s=>({kind:s.kind,duration:s.duration,label:s.label})));
});
document.getElementById('add-schedule-slot')?.addEventListener('click',()=>{
  if(!templateEditorSlots) templateEditorSlots=[];
  templateEditorSlots.push({kind:'rental', duration:1, label:'New Rental'});
  renderTemplateEditor(templateEditorSlots);
});
document.getElementById('reset-schedule-editor')?.addEventListener('click',()=>{ loadTemplateIntoEditor(); flash('Editor reset to current template.');});
document.getElementById('save-schedule-template')?.addEventListener('click',async()=>{
  const btn=document.getElementById('save-schedule-template'); if(!btn) return;
  btn.disabled=true;
  try{
    let cur=6; const payload=templateEditorSlots.map(s=>{const slot={start_hour:cur, duration:Number(s.duration), kind:s.kind, label:(s.label||'').trim()}; cur+=slot.duration; return slot;});
    const total=payload.reduce((a,b)=>a+b.duration,0);
    if(total!==18) throw new Error(`Total must be 18h (6 AM–12 MN), now ${total}h. Ayusin durations.`);
    const csrf=document.getElementById('schedule-editor') ? null : null;
    await api('/api/u/schedule/template', {slots: payload});
    flash('Repeating template saved. Applies every day until overridden.');
    await refreshSchedule();
    await loadTemplateIntoEditor();
  }catch(e){ flash(e.message,true); } finally{ btn.disabled=false; }
});
document.getElementById('schedule-override-editor')?.addEventListener('input',e=>{
  const idx=e.target.dataset.oidx; if(idx===undefined) return;
  const field=e.target.dataset.ofield; if(!field) return;
  if(field==='duration') overrideEditorSlots[Number(idx)].duration=Math.max(1,Math.min(12,Number(e.target.value)||1));
  else if(field==='kind') overrideEditorSlots[Number(idx)].kind=e.target.value;
  else if(field==='label') overrideEditorSlots[Number(idx)].label=e.target.value;
});
document.getElementById('schedule-override-editor')?.addEventListener('click',e=>{
  const del=e.target.dataset.delOv; if(del===undefined) return;
  overrideEditorSlots.splice(Number(del),1);
  renderOverrideEditor(overrideEditorSlots);
});
document.getElementById('add-override-slot')?.addEventListener('click',()=>{
  if(!overrideEditorSlots){
    // start from repeating template
    const rep=templateEditorSlots || [];
    overrideEditorSlots = rep.map(s=>({...s}));
  }
  overrideEditorSlots.push({kind:'rental', duration:1, label:'New'});
  renderOverrideEditor(overrideEditorSlots);
});
document.getElementById('load-override-day')?.addEventListener('click',async()=>{
  const day=document.getElementById('schedule-override-day')?.value;
  if(!day){ flash('Pick a date first.',true); return; }
  try{
    const data=await api(`/api/u/schedule/day?day=${day}`);
    // If there's an override, the day endpoint returns it; we need to know if it's override vs repeating
    // For now treat returned as editable override draft
    renderOverrideEditor(data.slots);
    document.getElementById('schedule-override-status').textContent=`Loaded ${day} — i-edit mo then Save override.`;
  }catch(e){ flash(e.message,true); }
});
document.getElementById('save-override-day')?.addEventListener('click',async()=>{
  const day=document.getElementById('schedule-override-day')?.value;
  if(!day){ flash('Pick an override day first.',true); return; }
  if(!overrideEditorSlots || !overrideEditorSlots.length){ flash('Load a day first.',true); return; }
  let cur=6; const payload=overrideEditorSlots.map(s=>{const slot={start_hour:cur, duration:Number(s.duration), kind:s.kind, label:(s.label||'').trim()}; cur+=slot.duration; return slot;});
  const total=payload.reduce((a,b)=>a+b.duration,0);
  if(total!==18){ flash(`Total must be 18h, now ${total}h.`,true); return; }
  try{ await api(`/api/u/schedule/day`, {day, slots: payload}); flash(`Override saved for ${day}.`); await refreshSchedule(); }catch(e){ flash(e.message,true); }
});
document.getElementById('clear-override-day')?.addEventListener('click',async()=>{
  const day=document.getElementById('schedule-override-day')?.value;
  if(!day){ flash('Pick a day to clear.',true); return; }
  try{ await api('/api/u/schedule/day/clear', {day}); flash(`Override cleared for ${day} — back to repeating.`); await refreshSchedule(); renderOverrideEditor(null); }catch(e){ flash(e.message,true); }
});
// Explicitly opt-in, fake-only manual checkout UI. A player claim is not proof;
// the reviewer must enter a separately checked FAKE-QR reference and amount.
// No real QR, transaction, email address or network mail transport is attached.
function syncManualTestMode(){
  const enabled=catalog?.fictional_manual_qr_enabled===true;
  $('#manual-my-panel').hidden=!enabled;
  $('#open-book-form button[type="submit"]').textContent=enabled?'Create fictional pending request →':'Simulate full payment →';
  $('#court-book-form button[type="submit"]').textContent=enabled?'Create fictional pending request →':'Simulate full payment →';
  $('#open-checkout-policy').textContent=enabled
    ?'Fictional pending requests do not hold a place. A fake reference claim is not proof; a permitted reviewer must separately check it. Manual review does not guarantee first-real-payment order.'
    :'When full, join for free. After a no-show, this prototype gives existing waiting-list accounts first access; the first simulated full payment takes the available place. This priority mechanism is provisional.';
  $('#court-availability-note').textContent=enabled
    ?'No provider fee is calculated. A fictional request does not hold the court; availability is checked again when reviewed.'
    :'No provider fee is calculated. Availability is checked atomically at simulated checkout.';
}
function manualRequestKey(form,payload){
  const signature=JSON.stringify(payload);
  if(form.dataset.testRequestSignature!==signature||!form.dataset.testRequestKey){
    if(!globalThis.crypto?.getRandomValues)throw new Error('A secure random test request key is unavailable.');
    const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);
    form.dataset.testRequestSignature=signature;
    form.dataset.testRequestKey='fake_'+[...bytes].map(x=>x.toString(16).padStart(2,'0')).join('');
  }
  return form.dataset.testRequestKey;
}
function clearManualRequestKey(form){delete form.dataset.testRequestSignature;delete form.dataset.testRequestKey;}
function clearManualReview(){manualEpoch++;$('#manual-review-list')?.replaceChildren();}
function manualState(item){
  if(item.state==='pending')return 'PENDING · no place held';
  if(item.state==='confirmed')return 'CONFIRMED · fictional only';
  if(item.state==='manual_review')return 'EXCEPTION · investigate';
  return 'CLOSED · fictional only';
}
function manualIssue(item){
  return ({amount_mismatch:'The checked fictional amount does not match. No booking was created.',
    paid_but_unbookable_or_changed:'The fictional slot, player or price changed after the request. No booking was created.'})[item.issue]||'The fictional exception needs investigation.';
}
function manualSchedule(item){
  const x=item.test_schedule||{};
  const kind=x.kind==='court'?`Court ${Number(x.court)||'?'} rental`:`Open Play${x.label?' · '+x.label:''}`;
  const when=x.day&&Number.isFinite(x.start_min)&&Number.isFinite(x.end_min)?`${x.day} · ${timeLabel(x.start_min)}–${timeLabel(x.end_min)}`:'Schedule unavailable';
  return `${kind} · ${when}`;
}
function manualClaimForm(item){
  if(item.state!=='pending')return '';
  return `<form class="stack manual-form" data-manual-claim="${Number(item.intent_id)}"><label>Fictional reference only<input name="reference" placeholder="FAKE-QR-example001" pattern="FAKE-QR-(?:[A-Za-z0-9_]|-){8,80}" maxlength="88" autocomplete="off" required></label><button type="submit" class="btn outline">${item.claim_submitted?'Update fake reference':'Submit fake reference'}</button></form>`;
}
async function refreshManualMine(){
  if(!isPlayer()||!catalog?.fictional_manual_qr_enabled)return;
  const uid=user.id,data=await api('/api/u/test-manual-qr/mine');
  if(!user||user.id!==uid||!isPlayer()||currentPage!=='bookings'||!catalog?.fictional_manual_qr_enabled)return;
  if(data.mode!=='fictional-only'||!Array.isArray(data.requests))throw new Error('Invalid fictional request response.');
  const holder=$('#manual-mine-list');
  if(!data.requests.length){holder.innerHTML='<div class="empty">No fictional manual-payment requests yet.</div>';return;}
  holder.innerHTML=data.requests.map(item=>{
    const id=Number(item.intent_id);if(!Number.isSafeInteger(id)||id<1)return '';
    const claim=item.claim_submitted?`Claim supplied · ends with ${escapeHTML(item.claim_suffix)}`:'No fake reference has been submitted.';
    const issue=item.state==='manual_review'?`<p class="helper"><b>Exception:</b> ${escapeHTML(manualIssue(item))}</p>`:'';
    return `<article class="manual-card"><div class="manual-card-head"><div><span class="alert-kind">${escapeHTML(manualState(item))}</span><h3>Test request #${id} · ${money(item.amount)}</h3></div></div><p>${escapeHTML(manualSchedule(item))}</p><p class="helper">${claim} This claim is not payment proof.</p>${issue}${manualClaimForm(item)}</article>`;
  }).join('')+(data.has_more?'<p class="helper">Showing the first 100 test requests.</p>':'');
}
function manualReviewForm(item){
  if(item.state!=='pending')return `<p class="helper"><b>Exception:</b> ${escapeHTML(manualIssue(item))} No resolution is implemented in this fictional lab.</p>`;
  if(!item.claim_submitted)return '<p class="helper">Waiting for the fictional player to submit a test reference. Do not confirm.</p>';
  return `<p class="helper">Player claim ends with <b>${escapeHTML(item.claim_suffix)}</b>. A claim or screenshot is not proof.</p><form class="stack manual-form" data-manual-review="${Number(item.intent_id)}"><label>Independently checked fictional reference<input name="reference" placeholder="FAKE-QR-example001" pattern="FAKE-QR-(?:[A-Za-z0-9_]|-){8,80}" maxlength="88" autocomplete="off" required></label><label>Independently observed fictional amount (PHP)<input type="number" name="observed_amount" min="1" max="100000000" step="1" required></label><label class="manual-check"><input type="checkbox" name="account_checked" required><span>I independently checked this fictional account-side reference and amount; I am not relying on a claim or screenshot.</span></label><button type="submit" class="btn primary">Submit fictional verification</button></form>`;
}
async function refreshManualReview(){
  if(!pageAllowed('manual'))return;
  const epoch=manualEpoch,uid=user.id,data=await api('/api/u/test-manual-qr/review');
  if(epoch!==manualEpoch||!user||user.id!==uid||currentPage!=='manual'||!pageAllowed('manual'))return;
  if(data.mode!=='fictional-only'||!Array.isArray(data.requests))throw new Error('Invalid fictional review response.');
  const holder=$('#manual-review-list');
  if(!data.requests.length){holder.innerHTML='<div class="empty">No fictional requests need review.</div>';return;}
  holder.innerHTML=data.requests.map(item=>{
    const id=Number(item.intent_id);if(!Number.isSafeInteger(id)||id<1)return '';
    const players=Array.isArray(item.test_players)?item.test_players.map(escapeHTML).join(', '):'Unavailable';
    return `<article class="manual-card"><span class="alert-kind">${escapeHTML(manualState(item))}</span><h3>Test request #${id} · ${money(item.amount)}</h3><p>${escapeHTML(manualSchedule(item))}</p><p class="helper">Fictional booker: ${escapeHTML(item.test_booker_name)} · Players: ${players}</p>${manualReviewForm(item)}</article>`;
  }).join('')+(data.has_more?'<p class="helper">Showing the first 100 test requests; more need operator attention.</p>':'');
}
$('#refresh-manual-mine').addEventListener('click',()=>refreshManualMine().catch(err=>flash(err.message,true)));
$('#refresh-manual-queue').addEventListener('click',()=>refreshManualReview().catch(err=>flash(err.message,true)));
$('#manual-mine-list').addEventListener('submit',async event=>{
  const form=event.target.closest('[data-manual-claim]');if(!form)return;event.preventDefault();
  const id=Number(form.dataset.manualClaim);if(!Number.isSafeInteger(id)||id<1)return;
  const button=form.querySelector('button[type="submit"]');button.disabled=true;
  try{const result=await api(`/api/u/test-manual-qr/${id}/claim`,{reference:form.elements.namedItem('reference').value});flash(result.message);await refreshManualMine();}
  catch(err){flash(err.message,true);}finally{button.disabled=false;}
});
$('#manual-review-list').addEventListener('submit',async event=>{
  const form=event.target.closest('[data-manual-review]');if(!form)return;event.preventDefault();
  const id=Number(form.dataset.manualReview);if(!Number.isSafeInteger(id)||id<1)return;
  const button=form.querySelector('button[type="submit"]');button.disabled=true;
  try{const result=await api(`/api/u/test-manual-qr/${id}/review`,{
    reference:form.elements.namedItem('reference').value,
    observed_amount:Number(form.elements.namedItem('observed_amount').value),
    account_checked:form.elements.namedItem('account_checked').checked===true});
    flash(result.message,result.state==='manual_review');await refreshManualReview();
  }catch(err){flash(err.message,true);}finally{button.disabled=false;}
});

async function refreshStaff(){const data=await api('/api/staff'),holder=$('#staff-list');holder.replaceChildren();if(!data.staff.length){holder.innerHTML='<div class="empty">No fictional staff accounts yet.</div>';return;}
  for(const p of data.staff){const section=document.createElement('section');section.className='staff-item';const name=document.createElement('strong');name.textContent=p.display_name;const sub=document.createElement('small');sub.textContent=`${p.email} · ${p.role} · ${p.enabled?'enabled':'disabled'}`;section.append(name,sub);const form=document.createElement('form'),grid=document.createElement('div');grid.className='permissions';
    for(const perm of data.grantable){const label=document.createElement('label');label.className='permission';const box=document.createElement('input');box.type='checkbox';box.value=perm;box.checked=p.permissions.includes(perm);box.disabled=!p.enabled;const span=document.createElement('span');span.textContent=perm==='payments.manual_verify'?`${perm} (${catalog?.fictional_manual_qr_enabled?'fictional test mode':'unmounted fictional lab'} only)`:perm;label.append(box,span);grid.append(label);}
    const save=document.createElement('button');save.type='submit';save.className='btn outline';save.textContent='Save permissions';save.disabled=!p.enabled;form.append(grid,save);section.append(form);form.addEventListener('submit',async e=>{e.preventDefault();save.disabled=true;try{await api(`/api/staff/${p.id}/permissions`,{permissions:[...grid.querySelectorAll('input:checked')].map(x=>x.value)});flash(`Permissions saved for ${p.display_name}.`);await refreshStaff();}catch(err){flash(err.message,true);}finally{save.disabled=false;}});
    if(p.enabled){const disable=document.createElement('button');disable.type='button';disable.className='btn danger gap';disable.textContent='Disable test account';disable.addEventListener('click',async()=>{if(!confirm(`Disable fictional account ${p.display_name}?`))return;disable.disabled=true;try{await api(`/api/staff/${p.id}/disable`,{});flash('Fictional account disabled and sessions revoked.');await refreshStaff();}catch(err){flash(err.message,true);}finally{disable.disabled=false;}});section.append(disable);}holder.append(section);}}
// Superuser-only fictional settings. The venue inbox is distinct from the
// sender and the player's own notice. No real account is configured or emailed.
function clearTestIntegrationSettings(){
  integrationEpoch++;integrationRevision=null;integrationDirty={sender:false,recipient:false};
  $('#test-sender-email').value='';$('#test-recipient-email').value='';
  $('#save-test-sender').disabled=true;$('#save-test-recipient').disabled=true;
  $('#replace-test-payment-qr').disabled=true;$('#test-venue-notices').replaceChildren();
  const image=$('#test-payment-qr-image');image.hidden=true;image.removeAttribute('src');
  $('#test-payment-qr-status').textContent='No fictional payment QR has been generated.';
}
function applyTestIntegrationSettings(data,epoch){
  if(epoch!==integrationEpoch||!isSuper()||currentPage!=='config')return;
  if(!Number.isSafeInteger(data.revision)||data.revision<0||data.mode!=='fictional-only')throw new Error('Invalid fictional settings response.');
  if(integrationRevision!==null&&data.revision<integrationRevision)return;
  if(data.payment_qr_image!==null&&!/^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(data.payment_qr_image))throw new Error('Invalid fictional QR image.');
  integrationRevision=data.revision;
  if(!integrationDirty.sender)$('#test-sender-email').value=data.sender_email||'';
  if(!integrationDirty.recipient)$('#test-recipient-email').value=data.booking_recipient_email||'';
  const image=$('#test-payment-qr-image');
  if(data.payment_qr_image){image.src=data.payment_qr_image;image.hidden=false;$('#test-payment-qr-status').textContent='Fictional image only. It cannot receive GCash or any payment.';}
  else{image.hidden=true;image.removeAttribute('src');$('#test-payment-qr-status').textContent='No fictional payment QR has been generated.';}
  $('#save-test-sender').disabled=false;$('#save-test-recipient').disabled=false;$('#replace-test-payment-qr').disabled=false;
}
function renderTestVenueNotices(data,epoch){
  if(epoch!==integrationEpoch||!isSuper()||currentPage!=='config')return;
  if(data.mode!=='fictional-only'||!Array.isArray(data.notices))throw new Error('Invalid fictional notices response.');
  const holder=$('#test-venue-notices');holder.replaceChildren();
  if(!data.notices.length){const empty=document.createElement('p');empty.className='helper';empty.textContent='No fictional venue notices yet.';holder.append(empty);return;}
  for(const item of data.notices){const card=document.createElement('div');card.className='staff-item';
    const title=document.createElement('b');title.textContent=item.subject;
    const details=document.createElement('p');details.textContent=item.body;
    const meta=document.createElement('small');meta.textContent=`Unsent test job · ${item.recipient} · ${item.state}`;
    card.append(title,details,meta);holder.append(card);
  }
}
async function refreshTestVenueNotices(){const epoch=integrationEpoch;const data=await api('/api/u/test-venue-booking-notices');renderTestVenueNotices(data,epoch);}
async function refreshTestIntegrationSettings(){
  const epoch=integrationEpoch;const [data,notices]=await Promise.all([api('/api/u/test-integration-settings'),api('/api/u/test-venue-booking-notices')]);
  applyTestIntegrationSettings(data,epoch);renderTestVenueNotices(notices,epoch);
}
async function updateTestIntegrationSettings(path,body,success,savedField=null){
  const epoch=integrationEpoch;
  if(!isSuper()||currentPage!=='config'||integrationRevision===null)return;
  const submittedValue=savedField==='sender'?body.sender_email:body.recipient_email;
  $('#save-test-sender').disabled=true;$('#save-test-recipient').disabled=true;$('#replace-test-payment-qr').disabled=true;
  try{const data=await api(path,{...body,expected_revision:integrationRevision});
    if(epoch===integrationEpoch&&isSuper()&&currentPage==='config'){
      if(savedField&&$(`#test-${savedField}-email`).value===submittedValue)integrationDirty[savedField]=false;
      applyTestIntegrationSettings(data,epoch);flash(success);
    }}
  catch(err){
    if(epoch===integrationEpoch&&isSuper()&&currentPage==='config'){
      flash(err.message,true);try{await refreshTestIntegrationSettings();}catch{clearTestIntegrationSettings();}
    }
  }
}
$('#test-sender-email').addEventListener('input',()=>{integrationDirty.sender=true;});
$('#test-recipient-email').addEventListener('input',()=>{integrationDirty.recipient=true;});
$('#test-sender-form').addEventListener('submit',e=>{e.preventDefault();updateTestIntegrationSettings('/api/u/test-integration-settings/sender',{sender_email:$('#test-sender-email').value},'Fictional sender saved. No real email account is connected.','sender');});
$('#test-recipient-form').addEventListener('submit',e=>{e.preventDefault();updateTestIntegrationSettings('/api/u/test-integration-settings/booking-recipient',{recipient_email:$('#test-recipient-email').value},'Fictional venue inbox saved. No email has been sent.','recipient');});
$('#replace-test-payment-qr').addEventListener('click',()=>{updateTestIntegrationSettings('/api/u/test-integration-settings/payment-qr',{},'Fictional payment QR replaced. It cannot receive payment.');});
$('#refresh-test-venue-notices').addEventListener('click',async()=>{try{await refreshTestVenueNotices();}catch(err){flash(err.message,true);}});
wireForm('#password-form',async form=>{const result=await api('/api/auth/password',formValues(form));form.reset();currentPage='home';await showAnonymous();flash(result.message);});
wireForm('#register-form',async form=>{const result=await api('/api/auth/register',formValues(form));form.reset();flash(result.message);});
wireForm('#login-form',async form=>{const result=await api('/api/auth/login',formValues(form));user={csrf:result.csrf};form.reset();currentPage='home';await loadIdentity();if(!linkedMatch())flash('Signed in to the fictional test portal.');});
wireForm('#open-book-form',async form=>{const payload={session_id:Number(form.elements.namedItem('session_id').value),player_ids:selectedPlayers($('#open-players'))};
  if(catalog?.fictional_manual_qr_enabled){const data=await api('/api/u/test-manual-qr/openplay',{...payload,idempotency_key:manualRequestKey(form,payload)});clearManualRequestKey(form);flash(`Fictional request #${data.intent_id} · ${money(data.amount)} pending. No place is held.`);await refreshCatalog();await navigate('bookings');}
  else{const data=await api('/api/u/book/openplay',payload);flash(`${data.reference}: ${money(data.amount)} simulated. No real charge.`);await refreshCatalog();}});
wireForm('#court-book-form',async form=>{const v=formValues(form),payload={day:v.day,start_hour:Number(v.start_hour),duration:Number(v.duration),court:Number(v.court),player_ids:selectedPlayers($('#court-players'))};
  if(catalog?.fictional_manual_qr_enabled){const data=await api('/api/u/test-manual-qr/court',{...payload,idempotency_key:manualRequestKey(form,payload)});clearManualRequestKey(form);flash(`Fictional request #${data.intent_id} · ${money(data.amount)} pending. No place is held.`);await refreshCatalog();await navigate('bookings');}
  else{const data=await api('/api/u/book/court',payload);flash(`${data.reference}: ${money(data.amount)} simulated. No real charge.`);await refreshCatalog();}});
wireForm('#rental-match-form',async form=>{const data=await api('/api/u/rental-match',{booking_id:Number(form.elements.namedItem('booking_id').value),player_ids:selectedPlayers($('#rental-players'))});flash(data.message);await navigate('matches');});
wireForm('#session-form',async form=>{const v=formValues(form),data=await api('/api/u/sessions',{...v,start_hour:Number(v.start_hour),duration:Number(v.duration)});form.reset();$('#session-day').value=catalog.today;flash(data.message);await refreshCatalog();});
wireForm('#rates-form',async form=>{const v=formValues(form),data=await api('/api/u/settings/rates',{open_rate:Number(v.open_rate),court_rate:Number(v.court_rate),paddle_rate:Number(v.paddle_rate)});flash(data.message);await refreshCatalog();});
wireForm('#limits-form',async form=>{const v=formValues(form),data=await api('/api/u/settings/limits',{open_limit:Number(v.open_limit),court_limit:Number(v.court_limit)});flash(data.message);await refreshCatalog();});
wireForm('#staff-form',async form=>{const data=await api('/api/staff',formValues(form));form.reset();flash(data.message);await refreshStaff();});
wireForm('#manual-form',async form=>{const v=formValues(form),data=await api('/api/u/queue/assign-selected',{session_id:Number($('#queue-session').value),court:Number(v.court),team_a:[Number(v.a1),Number(v.a2)],team_b:[Number(v.b1),Number(v.b2)]});flash(data.message);await refreshQueue();});
$('#navigation').addEventListener('click',e=>{const btn=e.target.closest('[data-page]');if(btn&&!btn.hidden){if(btn.dataset.page!=='matches'&&linkedMatch())history.replaceState(null,'',location.pathname+location.search);navigate(btn.dataset.page);}});
$$('[data-go]').forEach(btn=>btn.addEventListener('click',()=>{if(linkedMatch())history.replaceState(null,'',location.pathname+location.search);navigate(btn.dataset.go);}));
$('#refresh-player-alerts').addEventListener('click',()=>refreshNotices().catch(err=>flash(err.message,true)));
$('#player-alerts').addEventListener('click',async e=>{
  const btn=e.target.closest('[data-alert-id]');if(!btn||!isPlayer())return;
  const id=Number(btn.dataset.alertId),match=Number(btn.dataset.alertMatch),act=btn.dataset.alertAction;
  if(!Number.isSafeInteger(id)||id<1||!['read','match','ranking'].includes(act))return;
  btn.disabled=true;try{
    await api(`/api/u/player-alerts/${id}/read`,{});await refreshPlayerAlerts();
    if(act==='match'&&Number.isSafeInteger(match)&&match>0){history.replaceState(null,'',`#match-${match}`);await navigate('matches');focusLinkedMatch(match);}
    else if(act==='ranking'){history.replaceState(null,'',location.pathname+location.search);await navigate('ranking');}
  }catch(err){flash(err.message,true);}finally{btn.disabled=false;}
});
$('#notices-list').addEventListener('click',async e=>{const btn=e.target.closest('[data-open-test-match]');if(!btn)return;const id=Number(btn.dataset.openTestMatch);if(!Number.isSafeInteger(id)||id<1)return;history.replaceState(null,'',`#match-${id}`);await navigate('matches');focusLinkedMatch(id);});
window.addEventListener('hashchange',async()=>{const id=linkedMatch();if(id&&user&&pageAllowed('matches')){await navigate('matches');focusLinkedMatch(id);}});
$('#logout').addEventListener('click',async e=>{const btn=e.currentTarget;btn.disabled=true;try{await api('/api/auth/logout',{});currentPage='home';await showAnonymous();flash('Signed out of the fictional test account.');}catch(err){flash(err.message,true);}finally{btn.disabled=false;}});
$('#accept-notice').addEventListener('click',async e=>{const btn=e.currentTarget;btn.disabled=true;try{const data=await api('/api/u/notice/accept',{});user.notice_acknowledged=true;configureUI();flash(data.message);}catch(err){btn.disabled=false;flash(err.message,true);}});
$('#open-sessions').addEventListener('click',async e=>{const btn=e.target.closest('[data-wait]');if(!btn)return;btn.disabled=true;try{const data=await api('/api/u/waitlist',{session_id:Number(btn.dataset.wait)});flash(data.message);await refreshCatalog();}catch(err){btn.disabled=false;flash(err.message,true);}});
$('#rental-booking').addEventListener('change',renderRentalPlayers);
$('#queue-session').addEventListener('change',()=>{stopTestScanner();clearIssuedTestQr();refreshQueue().catch(e=>flash(e.message,true));});
$('#queue-list').addEventListener('click',async e=>{
  const button=e.target.closest('[data-queue-move]');if(!button||button.disabled||!can('queue.manage'))return;
  const sid=Number($('#queue-session').value),ids=queueSnapshot.map(p=>Number(p.id));
  const index=ids.indexOf(Number(button.dataset.queueId)),next=index+Number(button.dataset.queueMove);
  if(sid!==queueSnapshotSession||!queueRevision||index<0||next<0||next>=ids.length)return;
  const ordered=[...ids];[ordered[index],ordered[next]]=[ordered[next],ordered[index]];
  for(const el of $$('#queue-list [data-queue-move]'))el.disabled=true;
  try{const data=await api('/api/u/queue/reorder',{session_id:sid,expected_ids:ids,ordered_ids:ordered,expected_revision:queueRevision});flash(data.message);}
  catch(err){flash(err.message,true);}
  finally{try{await refreshQueue();}catch(err){flash(err.message,true);}}
});
// A real QR image and local decoder, restricted to fictional test accounts.
// QR challenges are short-lived but can still be photographed/shared; they do
// not prove a player is physically present and are not a production solution.
function clearIssuedTestQr(){
  if(testQrTimer){clearTimeout(testQrTimer);testQrTimer=null;}
  const image=$('#issued-test-qr');image.hidden=true;image.removeAttribute('src');$('#test-qr-expiry').textContent='';
}
function stopTestScanner(){
  testScanEpoch++;
  if(testScanFrame!==null){cancelAnimationFrame(testScanFrame);testScanFrame=null;}
  if(testScanStream){for(const track of testScanStream.getTracks())track.stop();testScanStream=null;}
  const video=$('#scan-preview');video.pause();video.srcObject=null;video.hidden=true;
  $('#stop-test-qr').hidden=true;$('#scan-test-qr').disabled=false;
}
async function acceptTestQr(raw){
  const parsed=/^LGSDTEST1:([1-9][0-9]{0,8}):([A-Za-z0-9_-]{32})$/.exec(String(raw));
  if(!parsed)throw new Error('This is not an LG Sports Deck fictional test QR.');
  const sid=Number(parsed[1]);
  if(sid!==Number($('#queue-session').value))throw new Error('Select the matching fictional session first.');
  const result=await api('/api/u/test-qr/check-in',{session_id:sid,challenge:parsed[2]});
  flash(result.message);await refreshQueue();
}
function readTestQrImage(source,width,height){
  if(typeof jsQR!=='function')throw new Error('The local test QR decoder is unavailable.');
  if(!width||!height||width>4096||height>4096||width*height>16777216)throw new Error('The test image is too large or invalid.');
  const scale=Math.min(1,1280/Math.max(width,height));const canvas=$('#scan-canvas');
  canvas.width=Math.max(1,Math.round(width*scale));canvas.height=Math.max(1,Math.round(height*scale));
  const ctx=canvas.getContext('2d',{willReadFrequently:true});if(!ctx)throw new Error('QR image decoding is unavailable.');
  ctx.drawImage(source,0,0,canvas.width,canvas.height);
  const pixels=ctx.getImageData(0,0,canvas.width,canvas.height);
  return jsQR(pixels.data,pixels.width,pixels.height,{inversionAttempts:'attemptBoth'});
}
async function startTestScanner(){
  stopTestScanner();const epoch=++testScanEpoch;
  if(!navigator.mediaDevices?.getUserMedia)throw new Error('Camera access needs a supported browser and HTTPS.');
  if(typeof jsQR!=='function')throw new Error('The local test QR decoder is unavailable.');
  $('#scan-test-qr').disabled=true;
  let stream;
  try{
    stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:'environment'}},audio:false});
    if(epoch!==testScanEpoch){for(const track of stream.getTracks())track.stop();return;}
    testScanStream=stream;const video=$('#scan-preview');video.srcObject=stream;video.hidden=false;
    $('#stop-test-qr').hidden=false;await video.play();
    if(epoch!==testScanEpoch)return;
    const tick=()=>{
      if(epoch!==testScanEpoch||!testScanStream)return;
      try{
        if(video.readyState>=2&&video.videoWidth&&video.videoHeight){
          const found=readTestQrImage(video,video.videoWidth,video.videoHeight);
          if(found){stopTestScanner();acceptTestQr(found.data).catch(err=>flash(err.message,true));return;}
        }
        testScanFrame=requestAnimationFrame(tick);
      }catch(err){stopTestScanner();flash(err.message,true);}
    };
    testScanFrame=requestAnimationFrame(tick);
  }catch(err){
    if(epoch===testScanEpoch)stopTestScanner();
    else if(stream)for(const track of stream.getTracks())track.stop();
    throw new Error('Unable to open the camera. Check browser permission or use a saved fictional QR image.');
  }
}
$('#issue-test-qr').addEventListener('click',async e=>{
  const button=e.currentTarget,sid=Number($('#queue-session').value);if(!sid){flash('Choose a fictional session first.',true);return;}
  button.disabled=true;clearIssuedTestQr();
  try{
    const data=await api('/api/u/test-qr/issue',{session_id:sid});
    if(!user||!can('attendance.qr.display')||currentPage!=='queue'||Number($('#queue-session').value)!==sid)return;
    if(!/^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(data.qr_image)||data.lifetime_seconds!==180)throw new Error('Invalid fictional QR response.');
    const image=$('#issued-test-qr');image.src=data.qr_image;image.hidden=false;
    $('#test-qr-expiry').textContent='Fictional QR issued. Expires in up to 3 minutes; the server checks the actual expiry.';
    testQrTimer=setTimeout(clearIssuedTestQr,data.lifetime_seconds*1000);flash('Fictional QR generated.');
  }catch(err){flash(err.message,true);}finally{button.disabled=false;}
});
$('#scan-test-qr').addEventListener('click',()=>{startTestScanner().catch(err=>flash(err.message,true));});
$('#stop-test-qr').addEventListener('click',stopTestScanner);
$('#scan-test-file').addEventListener('change',async e=>{
  const input=e.currentTarget,file=input.files?.[0];stopTestScanner();input.value='';
  if(!file)return;
  if(file.size>5*1024*1024||!['image/png','image/jpeg','image/webp'].includes(file.type)){flash('Choose a test PNG, JPEG or WebP image under 5 MB.',true);return;}
  try{
    // The test portal's CSP deliberately disallows blob: images. Read the
    // selected image locally as a data URL, which never uploads the image.
    const dataUrl=await new Promise((resolve,reject)=>{
      const reader=new FileReader();reader.onload=()=>resolve(reader.result);
      reader.onerror=()=>reject(new Error('Unable to read the fictional test image.'));reader.readAsDataURL(file);
    });
    const image=new Image();image.src=dataUrl;await image.decode();
    const found=readTestQrImage(image,image.naturalWidth,image.naturalHeight);
    image.removeAttribute('src');
    if(!found)throw new Error('No readable fictional QR was found in this image.');
    await acceptTestQr(found.data);
  }catch(err){flash(err.message||'Unable to read the fictional QR image.',true);}
});
document.addEventListener('visibilitychange',()=>{if(document.hidden)stopTestScanner();else if(isPlayer())refreshPlayerAlerts(true).catch(()=>{});});
window.addEventListener('pagehide',()=>{stopTestScanner();stopPlayerAlertPolling();});
window.addEventListener('pageshow',event=>{if(event.persisted&&isPlayer())startPlayerAlertPolling();});
$('#check-in').addEventListener('click',async e=>{const btn=e.currentTarget;btn.disabled=true;try{const data=await api('/api/u/check-in',{session_id:Number($('#queue-session').value),fictional_venue_token:$('#venue-token').value});flash(data.message);await refreshQueue();}catch(err){flash(err.message,true);}finally{btn.disabled=false;}});
$$('[data-assign]').forEach(btn=>btn.addEventListener('click',async()=>{btn.disabled=true;try{const data=await api('/api/u/queue/assign',{session_id:Number($('#queue-session').value),court:Number(btn.dataset.assign)});flash(data.message);await refreshQueue();}catch(err){flash(err.message,true);}finally{btn.disabled=false;}}));
$('#match-list').addEventListener('click',async e=>{const btn=e.target.closest('[data-match-action]');if(!btn)return;btn.disabled=true;try{const data=await api(`/api/u/matches/${btn.dataset.match}/${btn.dataset.matchAction}`,{});flash(data.previous_failed?'Previous synthetic clip failed; inspect test recorder.':data.message,data.previous_failed===true||data.state==='failed');await refreshMatches();}catch(err){flash(err.message,true);}finally{btn.disabled=false;}});
$('#match-list').addEventListener('submit',async e=>{const form=e.target.closest('[data-match-score]');if(!form)return;e.preventDefault();const btn=form.querySelector('button');btn.disabled=true;try{const v=formValues(form),data=await api(`/api/u/matches/${form.dataset.matchScore}/score`,{score_a:Number(v.score_a),score_b:Number(v.score_b)});flash(data.message);await refreshMatches();}catch(err){flash(err.message,true);}finally{btn.disabled=false;}});
$$('[data-sort]').forEach(btn=>btn.addEventListener('click',()=>{rankSort=btn.dataset.sort;refreshRanking().catch(err=>flash(err.message,true));}));
$('#load-roster').addEventListener('click',()=>refreshRoster().catch(err=>flash(err.message,true)));
$('#roster-list').addEventListener('click',async e=>{const btn=e.target.closest('[data-noshow]');if(!btn)return;if(!confirm('Mark this fictional player as a no-show? The simulated payment stays recorded.'))return;btn.disabled=true;try{const data=await api('/api/u/ops/no-show',{session_id:Number($('#roster-session').value),player_id:Number(btn.dataset.noshow)});flash(data.message);await refreshCatalog();await refreshRoster();}catch(err){flash(err.message,true);}finally{btn.disabled=false;}});
$$('[data-sales]').forEach(btn=>btn.addEventListener('click',()=>{salesPeriod=btn.dataset.sales;refreshSales().catch(err=>flash(err.message,true));}));
document.getElementById('refresh-schedule')?.addEventListener('click',()=>refreshSchedule().catch(e=>flash(e.message,true)));
document.getElementById('view-schedule-day')?.addEventListener('click',()=>refreshSchedule().catch(e=>flash(e.message,true)));
document.getElementById('cal-prev')?.addEventListener('click',()=>{ calMonth=new Date(calMonth.getFullYear(), calMonth.getMonth()-1,1); renderCalendar().catch(e=>flash(e.message,true)); });
document.getElementById('cal-next')?.addEventListener('click',()=>{ calMonth=new Date(calMonth.getFullYear(), calMonth.getMonth()+1,1); renderCalendar().catch(e=>flash(e.message,true)); });
fillHours();(async()=>{try{await loadIdentity();}catch(err){flash('Fictional test service unavailable. '+err.message,true);}})();
