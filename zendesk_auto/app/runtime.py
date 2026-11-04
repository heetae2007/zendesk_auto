"""Web login, which is also the Zendesk account the server works with.

One login for the whole server: whoever logs in last is the Zendesk account used for lookups and
intake, and automatic intake keeps using it after the browser is closed. A new login ends the
previous person's browser session; logging out ends every session and stops intake until someone
logs in again. Tokens are kept only in secrets/ on this server, never in Google Sheets or the browser.

Without an OAuth client in .env, the old ADMIN_PASSWORD login and ZENDESK_API_TOKEN keep working.
"""
import base64, hashlib, json, os, secrets, threading, time
from pathlib import Path
from urllib.parse import urlencode
import httpx
from .clients import IntegrationError

ACCESS_SECONDS=3600             # Zendesk allows 300..172,800
REFRESH_SECONDS=90*24*3600      # Zendesk maximum; every refresh issues a new refresh token
SESSION_SECONDS=12*3600         # browser cookie only; the Zendesk login stays until logout
STATE_SECONDS=600
COOKIE='easyedge_session';STATE_COOKIE='easyedge_oauth'
ROLES=('agent','admin')
_lock=threading.RLock();_pending={};_failures=[]

class LoginError(IntegrationError):pass

def path():return Path(os.getenv('SECRETS_DIR','./secrets'))/'zendesk_login.json'

def subdomain():
    s=os.getenv('ZENDESK_SUBDOMAIN','edgennext').strip()
    if not s.replace('-','').isalnum():raise ValueError('ZENDESK_SUBDOMAIN 오류')
    return s

def oauth():
    """OAuth client from .env, or None while it is not set up yet."""
    cid=os.getenv('ZENDESK_OAUTH_CLIENT_ID','').strip();secret=os.getenv('ZENDESK_OAUTH_CLIENT_SECRET','').strip()
    if not cid or not secret:return None
    base=os.getenv('PUBLIC_URL','http://127.0.0.1:8080').strip().rstrip('/')
    return {'client_id':cid,'client_secret':secret,'redirect_uri':base+'/auth/callback','secure':base.startswith('https://')}

def mode():return 'oauth' if oauth() else 'password'

def _hash(sid):return hashlib.sha256(sid.encode()).hexdigest()

def _saved():
    try:
        v=json.loads(path().read_text(encoding='utf-8'))
        return v if isinstance(v,dict) and v.get('mode')==mode() else None
    except (OSError,ValueError):
        return None

def _write(rec):
    p=path();p.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    tmp=p.with_suffix('.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(rec,f)
    os.replace(tmp,p)

def _expiry(seconds,default):
    if seconds is None and default is None:return None
    return time.time()+(seconds if isinstance(seconds,int) and seconds>0 else default)

# Browser sessions

def session_user(sid):
    rec=_saved()
    if not sid or not rec or not rec.get('sid') or rec.get('session_expires',0)<time.time():return None
    return rec['user'] if secrets.compare_digest(_hash(sid),rec['sid']) else None

def public():
    rec=_saved()
    return {'mode':mode(),'user':rec['user'] if rec else None,'since':rec.get('logged_in_at') if rec else None,
        'subdomain':subdomain()}

def _login(user,tokens=None):
    """Replace whatever login there was. Returns the new browser session id."""
    sid=secrets.token_urlsafe(32)
    with _lock:
        old=_saved()
        rec={'mode':mode(),'sid':_hash(sid),'session_expires':time.time()+SESSION_SECONDS,'user':user,
            'logged_in_at':time.strftime('%Y-%m-%dT%H:%M:%S%z')}
        if tokens:rec.update(_tokens(tokens))
        _write(rec)
    if old and old.get('access_token') and old['access_token']!=rec.get('access_token'):_revoke(old['access_token'])
    return sid

def logout():
    with _lock:
        rec=_saved()
        try:path().unlink()
        except FileNotFoundError:pass
    if rec and rec.get('access_token'):_revoke(rec['access_token'])

def password_login(password):
    """Fallback login while no OAuth client is set; Zendesk then uses ZENDESK_API_TOKEN."""
    expected=os.getenv('ADMIN_PASSWORD','')
    if len(expected)<16 or expected=='CHANGE_ME_TO_A_LONG_RANDOM_PASSWORD':raise LoginError('서버 ADMIN_PASSWORD를 16자 이상으로 설정하세요.')
    with _lock:
        now=time.time();_failures[:]=[t for t in _failures if t>now-300]
        if len(_failures)>=5:raise LoginError('비밀번호를 여러 번 틀렸습니다. 5분 뒤 다시 시도하세요.')
        if not secrets.compare_digest(str(password or '').encode(),expected.encode()):
            _failures.append(now);raise LoginError('비밀번호가 올바르지 않습니다.')
    return _login({'name':'관리자','email':'','role':'admin'})

# Zendesk OAuth (authorization code with PKCE)

def start():
    """Zendesk authorization URL and the state value the browser must bring back."""
    o=oauth()
    if not o:raise LoginError('Zendesk OAuth 클라이언트가 설정되지 않았습니다.')
    state=secrets.token_urlsafe(24);verifier=secrets.token_urlsafe(48)
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    with _lock:
        now=time.time()
        for k in [k for k,v in _pending.items() if v[1]<now]:_pending.pop(k)
        _pending[state]=(verifier,now+STATE_SECONDS)
    query=urlencode({'response_type':'code','client_id':o['client_id'],'redirect_uri':o['redirect_uri'],'scope':'read write',
        'state':state,'code_challenge':challenge,'code_challenge_method':'S256'})
    return f'https://{subdomain()}.zendesk.com/oauth/authorizations/new?{query}',state

def finish(code,state,browser_state):
    """Exchange the code, check the person is an agent or admin, and make them the server's login."""
    o=oauth()
    with _lock:item=_pending.pop(str(state or ''),None)
    # The state must also match this browser's cookie, so nobody can push their login into another browser.
    if not o or not item or item[1]<time.time() or not code or not browser_state or not secrets.compare_digest(str(browser_state),str(state)):
        raise LoginError('expired')
    tokens=_token_request({'grant_type':'authorization_code','code':code,'redirect_uri':o['redirect_uri'],'code_verifier':item[0],
        'scope':'read write','expires_in':ACCESS_SECONDS,'refresh_token_expires_in':REFRESH_SECONDS})
    user=_me(tokens['access_token'])
    if user['role'] not in ROLES:
        _revoke(tokens['access_token']);raise LoginError('role')
    return _login(user,tokens),user

def _tokens(t):
    return {'access_token':t['access_token'],'refresh_token':t.get('refresh_token'),
        'expires_at':_expiry(t.get('expires_in'),ACCESS_SECONDS if t.get('refresh_token') else None)}

def _token_request(payload):
    o=oauth()
    try:r=httpx.post(f'https://{subdomain()}.zendesk.com/oauth/tokens',json={**payload,'client_id':o['client_id'],'client_secret':o['client_secret']},timeout=25)
    except httpx.RequestError:raise LoginError('Zendesk 연결 오류') from None
    if r.status_code>=400:
        e=LoginError(f'Zendesk 로그인 거부 HTTP {r.status_code}');e.status=r.status_code;raise e
    t=r.json()
    if not isinstance(t.get('access_token'),str) or not t['access_token']:raise LoginError('Zendesk 토큰 응답 오류')
    return t

def _me(token):
    try:r=httpx.get(f'https://{subdomain()}.zendesk.com/api/v2/users/me.json',headers={'Authorization':'Bearer '+token},timeout=25)
    except httpx.RequestError:raise LoginError('Zendesk 연결 오류') from None
    u=r.json().get('user',{}) if r.status_code<400 else {}
    # A bad token can come back as an anonymous user, so require a real id.
    if not u.get('id'):raise LoginError('Zendesk 사용자 확인 실패')
    return {'id':u['id'],'name':u.get('name',''),'email':u.get('email',''),'role':u.get('role','')}

def _revoke(token):
    try:httpx.delete(f'https://{subdomain()}.zendesk.com/api/v2/oauth/tokens/current.json',headers={'Authorization':'Bearer '+token},timeout=10)
    except httpx.RequestError:pass

def access_token():
    """Current access token, refreshed five minutes before it expires. None when nobody is logged in."""
    with _lock:
        rec=_saved()
        if not rec or not rec.get('access_token'):return None
        if rec.get('expires_at') and rec['expires_at']-time.time()<300:
            try:t=_token_request({'grant_type':'refresh_token','refresh_token':rec.get('refresh_token') or '',
                'expires_in':ACCESS_SECONDS,'refresh_token_expires_in':REFRESH_SECONDS})
            except LoginError as e:
                # A rejected refresh means the login is gone for good; a network error is tried again next time.
                if getattr(e,'status',None) in (400,401):
                    try:path().unlink()
                    except FileNotFoundError:pass
                    raise LoginError('Zendesk 로그인이 만료되었습니다. 다시 로그인하세요.') from None
                raise
            rec.update(_tokens(t));_write(rec)
        return rec['access_token']

class BearerAuth(httpx.Auth):
    def auth_flow(self,request):
        token=access_token()
        if not token:raise LoginError('Zendesk 로그인이 필요합니다.')
        request.headers['Authorization']='Bearer '+token
        yield request

def zendesk_auth():
    """httpx auth for Zendesk API calls: the logged-in person, or the .env API token without OAuth."""
    if oauth():return BearerAuth()
    token=os.getenv('ZENDESK_API_TOKEN','')
    return (os.getenv('ZENDESK_EMAIL','')+'/token',token) if token else None

def zendesk_ready():
    if oauth():
        rec=_saved();return bool(rec and rec.get('access_token'))
    return bool(os.getenv('ZENDESK_API_TOKEN'))
