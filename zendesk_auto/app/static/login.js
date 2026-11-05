const $=s=>document.querySelector(s),e=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ERRORS={denied:'Zendesk에서 로그인을 취소했습니다.',expired:'로그인 요청이 만료되었습니다. 다시 로그인하세요.',role:'Zendesk 상담원·관리자 계정만 로그인할 수 있습니다.',busy:'분석이 진행 중입니다. 잠시 후 다시 로그인하세요.',failed:'Zendesk 로그인에 실패했습니다. 잠시 후 다시 시도하세요.'};
async function show(){
 const info=await (await fetch('/auth/info')).json(),err=ERRORS[new URLSearchParams(location.search).get('e')];
 const note=err?`<div class="notice warn">${e(err)}</div>`:'';
 if(info.mode==='oauth'){
  $('#login').innerHTML=`${note}<p>Zendesk(${e(info.subdomain)}) 계정으로 로그인합니다.</p>
   <a class="button primary wide" href="/auth/login">Zendesk로 로그인</a>
   <p class="help">${info.current?`현재 <b>${e(info.current)}</b> 계정으로 자동 접수 중입니다.`:info.intake_paused?'접수 계정이 없어 자동 접수가 멈춰 있습니다.':''}</p>`;
  return}
 $('#login').innerHTML=`${note}<form id="pw"><label>관리자 비밀번호<input name="password" type="password" autocomplete="current-password" required autofocus></label><button class="primary wide">로그인</button><p class="help" id="msg"></p></form>`;
 $('#pw').addEventListener('submit',async ev=>{ev.preventDefault();
  const r=await fetch('/auth/password',{method:'POST',headers:{'X-Requested-With':'zendesk-admin','Content-Type':'application/json'},body:JSON.stringify({password:ev.target.password.value})});
  if(r.ok)location.href='/';else $('#msg').textContent=(await r.json()).detail||'로그인 실패'})
}
show().catch(()=>{$('#login').innerHTML='<p class="help">서버 연결을 확인해 주세요.</p>'});
