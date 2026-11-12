'use strict';
let auth=null,anonToken=null;
const $=s=>document.querySelector(s);
const notice=(text,error=false)=>{const el=$('#message');el.textContent=text;el.classList.toggle('error',error);el.hidden=false;};
const text=(tag,value,cls='')=>{const el=document.createElement(tag);el.textContent=value;if(cls)el.className=cls;return el;};
const headers=()=>({'Content-Type':'application/json','X-CSRF-Token':auth?auth.csrf:anonToken});
async function api(path,body){
  const options=body===undefined?{credentials:'same-origin'}:{method:'POST',credentials:'same-origin',headers:headers(),body:JSON.stringify(body)};
  const response=await fetch(path,options);let data={};try{data=await response.json();}catch{}
  if(!response.ok){const detail=typeof data.detail==='string'?data.detail:'Check the fictional test details and try again.';throw new Error(detail);}
  return data;
}
function wireForm(selector,fn){
  $(selector).addEventListener('submit',async e=>{e.preventDefault();const form=e.currentTarget,button=form.querySelector('button[type=submit]');if(button)button.disabled=true;
    try{await fn(form);}catch(err){notice(err.message,true);}finally{if(button)button.disabled=false;}});
}
function values(form){return Object.fromEntries(new FormData(form).entries());}
async function anonymous(){auth=null;const response=await api('/api/auth/bootstrap');anonToken=response.csrf;$('#auth-view').hidden=false;$('#account-view').hidden=true;}
async function refreshSession(){const state=await api('/api/auth/state');if(!state.authenticated){await anonymous();return;}auth=state;$('#auth-view').hidden=true;$('#account-view').hidden=false;
  $('#account-name').textContent=`Welcome, ${auth.name}.`;$('#account-summary').textContent=`${auth.email} · fictional ${auth.role} account`;
  $('#session-role').textContent=`Signed in as ${auth.role}. This identity cannot be switched without signing in again.`;
  $('#session-permissions').textContent=auth.role==='superuser'?'Superuser: test staff controls and private replay access.':auth.role==='player'?'Player: only this account’s own test actions and participant replays.':`Granted test actions: ${auth.permissions.length?auth.permissions.join(', '):'none'}.`;
  $('#player-panel').hidden=auth.role!=='player';$('#staff-panel').hidden=auth.role!=='superuser';
  const canBookings=auth.role==='superuser'||auth.permissions.includes('bookings.view');
  const canSessions=auth.role==='superuser'||auth.permissions.includes('session.view');
  $('#ops-panel').hidden=!canBookings&&!canSessions;$('#view-bookings').hidden=!canBookings;$('#view-sessions').hidden=!canSessions;
  if(auth.role==='player')await refreshPlayer();
  if(auth.role==='superuser')await refreshStaff();
}
async function refreshPlayer(){
  $('#notice-status').textContent=auth.notice_acknowledged?'Your fictional notice is acknowledged.':'Acknowledge the fictional notice for this account before a test booking.';
  $('#accept-notice').disabled=auth.notice_acknowledged;
  const [sessionData,playerData]=await Promise.all([api('/api/test/sessions'),api('/api/test/players')]);
  const select=$('#test-session');select.replaceChildren();
  if(!sessionData.sessions.length){const option=text('option','No fictional session configured');option.value='';select.append(option);}
  for(const s of sessionData.sessions){const option=text('option',`${s.label} · ${s.occupied}/${s.capacity} sample places`);option.value=String(s.id);select.append(option);}
  const holder=$('#test-players');holder.replaceChildren();
  for(const p of playerData.players){const label=document.createElement('label');label.className='player';const cb=document.createElement('input');cb.type='checkbox';cb.name='player';cb.value=String(p.id);if(p.id===auth.id){cb.checked=true;cb.disabled=true;}label.append(cb,text('span',p.display_name+(p.id===auth.id?' (you)':'')));holder.append(label);}
}
async function refreshStaff(){
  const data=await api('/api/staff'),holder=$('#staff-list');holder.replaceChildren();
  if(!data.staff.length){holder.append(text('p','No fictional staff accounts yet.','helper'));return;}
  for(const person of data.staff){
    const section=document.createElement('section');section.className='staff-item';section.append(text('strong',person.display_name));section.append(text('small',`${person.email} · ${person.role} · ${person.enabled?'enabled':'disabled'}`));
    const form=document.createElement('form'),grid=document.createElement('div');grid.className='permissions';
    for(const permission of data.grantable){const label=document.createElement('label');label.className='permission';const cb=document.createElement('input');cb.type='checkbox';cb.value=permission;cb.name='permissions';cb.checked=person.permissions.includes(permission);cb.disabled=!person.enabled;label.append(cb,text('span',permission));grid.append(label);}
    const save=text('button','Save permissions','outline');save.type='submit';save.disabled=!person.enabled;form.append(grid,save);section.append(form);
    form.addEventListener('submit',async e=>{e.preventDefault();save.disabled=true;try{const permissions=[...grid.querySelectorAll('input:checked')].map(x=>x.value);await api(`/api/staff/${person.id}/permissions`,{permissions});notice(`Permissions updated for ${person.display_name}.`);await refreshStaff();}catch(err){notice(err.message,true);}finally{save.disabled=false;}});
    if(person.enabled){const disable=text('button','Disable test account','outline');disable.type='button';disable.addEventListener('click',async()=>{if(!confirm(`Disable fictional account ${person.display_name}?`))return;disable.disabled=true;try{await api(`/api/staff/${person.id}/disable`,{});notice('Fictional staff account disabled and sessions revoked.');await refreshStaff();}catch(err){notice(err.message,true);}finally{disable.disabled=false;}});section.append(disable);}
    holder.append(section);
  }
}
wireForm('#login-form',async form=>{const result=await api('/api/auth/login',values(form));anonToken=null;auth={csrf:result.csrf};form.reset();await refreshSession();notice('Signed in to the fictional account.');});
wireForm('#register-form',async form=>{const result=await api('/api/auth/register',values(form));form.reset();notice(result.message);});
wireForm('#password-form',async form=>{const result=await api('/api/auth/password',values(form));form.reset();await anonymous();notice(result.message);});
wireForm('#staff-create-form',async form=>{const result=await api('/api/staff',values(form));form.reset();await refreshStaff();notice(result.message);});
wireForm('#checkout-form',async()=>{const session_id=Number($('#test-session').value);const companions=[...document.querySelectorAll('#test-players input:checked')].filter(x=>!x.disabled).map(x=>Number(x.value));
  const result=await api('/api/test/checkout',{session_id,player_ids:[auth.id,...companions]});notice(`TEST-${result.booking_id}: ₱${result.amount} simulated. No real charge or booking.`);await refreshPlayer();});
$('#accept-notice').addEventListener('click',async e=>{const button=e.currentTarget;button.disabled=true;try{const result=await api('/api/test/notice',{});auth.notice_acknowledged=true;await refreshPlayer();notice(result.message);}catch(err){button.disabled=false;notice(err.message,true);}});
$('#logout').addEventListener('click',async e=>{const button=e.currentTarget;button.disabled=true;try{await api('/api/auth/logout',{});await anonymous();notice('Signed out.');}catch(err){notice(err.message,true);}finally{button.disabled=false;}});
$('#view-bookings').addEventListener('click',async()=>{try{const data=await api('/api/ops/test-bookings');$('#ops-result').textContent=`${data.bookings.length} sample booking(s). Amounts and payments are simulated.`;}catch(err){notice(err.message,true);}});
$('#view-sessions').addEventListener('click',async()=>{try{const data=await api('/api/test/sessions');$('#ops-result').textContent=`${data.sessions.length} fictional session(s) configured.`;}catch(err){notice(err.message,true);}});
(async()=>{try{await refreshSession();}catch(err){notice('Test service unavailable. '+err.message,true);}})();
