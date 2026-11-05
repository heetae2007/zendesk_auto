import os, json
os.environ['ADMIN_PASSWORD']='test-password-not-for-production'
os.environ['DATA_DIR']='/tmp/zendesk-web-tests'
# Never touch the real Sheets store or allow live writes from tests, whatever .env says.
os.environ['STORE_MODE']='local'
os.environ['ALLOW_LIVE_WRITES']='false'
os.environ['SECRETS_DIR']='/tmp/zendesk-web-tests/secrets'
# Password-login mode unless a test sets up OAuth itself, whatever .env says.
os.environ['ZENDESK_OAUTH_CLIENT_ID']='';os.environ['ZENDESK_OAUTH_CLIENT_SECRET']=''
from fastapi.testclient import TestClient
from app.main import app

def logged_in():
    client=TestClient(app)
    assert client.post('/auth/password',json={'password':os.environ['ADMIN_PASSWORD']},headers={'X-Requested-With':'zendesk-admin'}).status_code==200
    return client

def test_auth_and_csrf():
    client=TestClient(app)
    assert client.get('/api/state').status_code==401
    assert client.get('/',follow_redirects=False).headers['location']=='/login'
    assert client.get('/login').status_code==200
    client=logged_in()
    r=client.get('/api/state');assert r.status_code==200
    assert r.json()['mode']=='추천 전용'
    assert client.post('/api/run',json={}).status_code==403
    assert client.get('/').status_code==200
    assert client.get('/static/app.js').status_code==200

def test_live_update_endpoint_absent():
    paths={r.path for r in app.routes}
    assert '/api/assign' not in paths and '/api/live' not in paths

H={'X-Requested-With':'zendesk-admin'}

def test_schedule_read_returns_review_rows_only(monkeypatch):
    from app.main import engine,store
    before=store.config.get('settings',{}).get('pis_days')
    monkeypatch.setattr(engine.ai,'read_schedule',lambda data,kind,names,settings=None:{'days':[
        {'date_text':'#VALUE!','month':None,'day':None,'call1':['이혜경'],'call2':[],'call3':[],'leave':[],'notes':'','uncertain':True}],'warnings':[]})
    client=logged_in()
    png=b'\x89PNG\r\n\x1a\n'+b'0'*20
    r=client.post('/api/pis/schedule/read?year=2026',files={'file':('a.png',png,'image/png')},headers=H)
    assert r.status_code==200 and r.json()['days'][0]['date'] is None
    assert store.config['settings'].get('pis_days')==before
    r=client.post('/api/pis/schedule/read',files={'file':('a.gif',b'GIF89a','image/gif')},headers=H)
    assert r.status_code==400

def test_state_exposes_pis_and_live_mode():
    r=logged_in().get('/api/state').json()
    assert r['live']=={'enabled':False,'env_allows':False,'setting':False}
    assert 'on_call' in r['pis_today'] and len(r['pis_members'])>=13

def test_config_save_validates_oncall(monkeypatch):
    from app.main import store
    from app.store import revision
    import copy
    client=logged_in();c=copy.deepcopy(store.config)
    c['settings']['pis_days']=[{'date':'2099-01-01','members':[{'name':'외부인','slot':'call1','weight':1,'absent':False}]}]
    assert client.post('/api/config',json={'config':c,'revision':revision(store.config)},headers=H).status_code==409
    c['settings']['pis_days'][0]['members'][0]['name']='이혜경'
    c['settings']['pis_days'].append({'date':'2000-01-01','members':[{'name':'이혜경','slot':'call1','weight':1,'absent':False}]})
    assert client.post('/api/config',json={'config':c,'revision':revision(store.config)},headers=H).status_code==200
    assert [d['date'] for d in store.config['settings']['pis_days']]==['2099-01-01']

def test_retry_refuses_after_uncertain_write():
    from app.main import store
    store.set_job(77,status='review');store.state.setdefault('writes',{})['77']={'status':'unknown'}
    r=logged_in().post('/api/jobs/77/retry',json={},headers=H)
    assert r.status_code==409

def test_work_mapping_import_ignores_parts_outside_catalogue():
    from io import BytesIO
    from openpyxl import Workbook
    from app.main import store
    store.config['settings']['parts']=[{'name':'보험','active':True,'option_value':'','developer':'','developer_backup':'','pis':'','pis_backup':''}]
    wb=Workbook();s=wb.active
    s.append(['','메뉴','','담당자','1차백업','담당자','1차백업'])
    s.append(['','보험','심사','채희태','이정기','노주영','이혜경'])
    s.append(['','수납','수납관리','김구현','','',''])
    buf=BytesIO();wb.save(buf)
    r=logged_in().post('/api/work-mapping/import',files={'file':('m.xlsx',buf.getvalue(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')},headers=H)
    assert r.status_code==200 and [x['work'] for x in r.json()['rows']]==['심사'] and r.json()['ignored']==['수납']

def test_removed_endpoints_are_gone():
    paths={r.path for r in app.routes}
    assert not {'/api/import','/api/analytics/review/{tid}','/api/pis/preview','/api/pis/backlog-preview'}&paths

def test_models_from_screen_override_env(monkeypatch):
    from app.clients import model_for, effort_for
    from app.domain import validate_config
    from test_core import config
    monkeypatch.setenv('ANTHROPIC_MODEL','claude-sonnet-4-6');monkeypatch.setenv('ANTHROPIC_MODEL_TICKET','claude-haiku-5-5')
    for n in ('ANTHROPIC_MODEL_IMAGE','ANTHROPIC_MODEL_SCHEDULE','ANTHROPIC_EFFORT'):monkeypatch.delenv(n,raising=False)
    s={'models':{'ticket':'','image':'claude-sonnet-5-5','effort':'medium'}}
    assert model_for('ticket',s)=='claude-haiku-5-5' and model_for('ticket_attachment',s)=='claude-sonnet-5-5' and model_for('pis_schedule')=='claude-sonnet-4-6'
    assert effort_for('claude-sonnet-5-5',s)=='medium' and effort_for('claude-sonnet-4-6',s) is None
    c=config();c['settings']['models']={'ticket':'gpt-4'}
    import pytest
    with pytest.raises(ValueError,match='지원하지 않는 모델'):validate_config(c)

H2={'X-Requested-With':'zendesk-admin'}

def test_password_login_many_sessions_logout_ends_only_one():
    first=logged_in();second=logged_in()
    # Both stay logged in; logging out ends only the browser that asked.
    assert first.get('/api/state').status_code==200 and second.get('/api/state').status_code==200
    assert TestClient(app).post('/auth/password',json={'password':'wrong'},headers=H2).status_code==401
    assert second.post('/auth/logout',headers=H2).status_code==200
    assert second.get('/api/state').status_code==401 and first.get('/api/state').status_code==200

def test_zendesk_login_is_the_intake_account(monkeypatch,tmp_path):
    import time
    from urllib.parse import urlparse,parse_qs
    from app import runtime
    from app.main import engine
    monkeypatch.setenv('SECRETS_DIR',str(tmp_path))
    monkeypatch.setenv('ZENDESK_OAUTH_CLIENT_ID','easyedge');monkeypatch.setenv('ZENDESK_OAUTH_CLIENT_SECRET','client-secret')
    monkeypatch.setenv('PUBLIC_URL','https://easyedge.example')
    calls=[];people={'tok-1':{'id':1,'name':'김상담','email':'a@example.com','role':'agent'},'tok-2':{'id':2,'name':'고객','email':'c@example.com','role':'end-user'},'tok-3':{'id':3,'name':'박상담','email':'b@example.com','role':'agent'}}
    def token_request(payload):
        calls.append(payload)
        if payload['grant_type']=='refresh_token':
            if payload['refresh_token']=='dead':
                e=runtime.LoginError('Zendesk 로그인 거부 HTTP 400');e.status=400;raise e
            return {'access_token':'tok-1b','refresh_token':'ref-1b','expires_in':3600}
        return {'access_token':payload['code'].replace('code','tok'),'refresh_token':'ref','expires_in':3600}
    revoked=[]
    monkeypatch.setattr(runtime,'_token_request',token_request)
    monkeypatch.setattr(runtime,'_me',lambda t:people[t])
    monkeypatch.setattr(runtime,'_revoke',revoked.append)
    def login(code):
        c=TestClient(app,base_url='https://easyedge.example')
        r=c.get('/auth/login',follow_redirects=False);url=urlparse(r.headers['location']);q=parse_qs(url.query)
        assert url.netloc=='edgennext.zendesk.com' and q['redirect_uri']==['https://easyedge.example/auth/callback'] and q['code_challenge_method']==['S256']
        assert q['scope']==['read tickets:write'] and 'scope=read%20tickets%3Awrite' in url.query
        return c,c.get(f"/auth/callback?code={code}&state={q['state'][0]}",follow_redirects=False)
    assert not engine.z.ready
    # A state that did not start in this browser is refused.
    c=TestClient(app,base_url='https://easyedge.example')
    assert c.get('/auth/callback?code=code-1&state=forged',follow_redirects=False).headers['location']=='/login?e=expired'
    c,r=login('code-2')                                    # customers cannot log in
    assert r.headers['location']=='/login?e=role' and revoked==['tok-2'] and not engine.z.ready
    c,r=login('code-1')                                    # first login while nobody holds intake: becomes the intake account
    assert r.headers['location']=='/' and 'secure' in r.headers['set-cookie'].lower()
    assert calls[-1]['code_verifier'] and calls[-1]['expires_in']==3600
    assert oct(runtime.path().stat().st_mode)[-3:]=='600' and engine.z.ready
    state=c.get('/api/state').json()
    assert state['login']['user']['name']=='김상담' and state['login']['me']['name']=='김상담'
    assert 'tok-1' not in json.dumps(state) and 'ref' not in json.dumps(state['login'])
    assert TestClient(app).get('/auth/info').json()=={'mode':'oauth','subdomain':'edgennext','current':'김상담','intake_paused':False}
    # A second person logs in at the same time: both sessions work, the intake account stays, their token is revoked.
    d,r=login('code-3')
    assert r.headers['location']=='/' and revoked[-1]=='tok-3'
    assert c.get('/api/state').status_code==200 and d.get('/api/state').json()['login']['me']['name']=='박상담'
    assert d.get('/api/state').json()['login']['user']['name']=='김상담' and runtime.access_token()=='tok-1'
    # Choosing the intake account needs a login; then the previous token is revoked.
    assert TestClient(app,base_url='https://easyedge.example').get('/auth/login?intake=1',follow_redirects=False).headers['location']=='/login'
    r=d.get('/auth/login?intake=1',follow_redirects=False);q=parse_qs(urlparse(r.headers['location']).query)
    r=d.get(f"/auth/callback?code=code-3&state={q['state'][0]}",follow_redirects=False)
    assert d.get('/api/state').json()['login']['user']['name']=='박상담' and revoked[-1]=='tok-1' and runtime.access_token()=='tok-3'
    assert c.get('/api/state').status_code==200
    # Shortly before expiry the token is refreshed and saved.
    rec=json.loads(runtime.path().read_text());rec['expires_at']=time.time();runtime._write(rec)
    assert runtime.access_token()=='tok-1b' and json.loads(runtime.path().read_text())['refresh_token']=='ref-1b'
    # A refresh Zendesk rejects releases the intake account; web sessions stay.
    rec=json.loads(runtime.path().read_text());rec.update(expires_at=time.time(),refresh_token='dead');runtime._write(rec)
    import pytest
    with pytest.raises(runtime.LoginError,match='만료'):runtime.access_token()
    assert not engine.z.ready and c.get('/api/state').status_code==200
    # Logout ends only that browser and leaves intake alone; releasing the intake account pauses it.
    e,r=login('code-1')                                    # nobody holds intake again, so this one takes it
    assert engine.z.ready and e.post('/auth/logout',headers=H2).status_code==200
    assert e.get('/api/state').status_code==401 and c.get('/api/state').status_code==200 and engine.z.ready
    assert c.post('/api/intake/release',headers=H2).status_code==200
    assert revoked[-1]=='tok-1' and not engine.z.ready and c.get('/api/state').status_code==200
    assert TestClient(app).get('/auth/info').json()['intake_paused'] is True

def test_zendesk_calls_use_the_logged_in_token(monkeypatch,tmp_path):
    import time,httpx
    from app import runtime,clients
    monkeypatch.setenv('SECRETS_DIR',str(tmp_path))
    monkeypatch.setenv('ZENDESK_OAUTH_CLIENT_ID','easyedge');monkeypatch.setenv('ZENDESK_OAUTH_CLIENT_SECRET','client-secret')
    seen=[];real=httpx.Client
    transport=httpx.MockTransport(lambda req:seen.append(req.headers.get('authorization')) or httpx.Response(200,json={'ticket':{'id':5}}))
    monkeypatch.setattr(httpx,'Client',lambda **k:real(transport=transport,**k))
    z=clients.Zendesk()
    import pytest
    with pytest.raises(clients.IntegrationError,match='로그인'):z.ticket(5)   # nobody logged in: no call at all
    assert seen==[]
    runtime._write({'mode':'oauth','sid':'x','session_expires':0,'user':{'name':'김상담'},'access_token':'tok-9','refresh_token':'r','expires_at':time.time()+3600})
    assert z.ticket(5)=={'id':5} and seen==['Bearer tok-9']

def test_daily_counts_by_kst_creation_date():
    from datetime import date
    from app.main import daily_counts
    jobs=[{'created_at':'2026-11-03T15:30:00Z','status':'review'},          # 11-04 00:30 KST
        {'created_at':'2026-11-03T14:59:00Z','status':'recommended','write_status':'done'},
        {'created_at':'2026-11-04T01:00:00Z','status':'queued'},{'created_at':None,'status':'review'}]
    rows=daily_counts(jobs,date(2026,11,4),days=3)
    assert [r['date'] for r in rows]==['2026-11-04','2026-11-03','2026-11-02']
    assert rows[0]|{}=={'date':'2026-11-04','total':2,'recommended':0,'review':1,'deferred':0,'other':1,'written':0}
    assert rows[1]['recommended']==1 and rows[1]['written']==1 and rows[2]['total']==0

def test_oauth_uses_the_address_the_browser_opened(monkeypatch,tmp_path):
    from urllib.parse import urlparse,parse_qs
    from app import runtime
    monkeypatch.setenv('SECRETS_DIR',str(tmp_path))
    monkeypatch.setenv('ZENDESK_OAUTH_CLIENT_ID','easyedge');monkeypatch.setenv('ZENDESK_OAUTH_CLIENT_SECRET','client-secret')
    monkeypatch.setenv('PUBLIC_URL','http://localhost:8080, http://192.168.0.10:8080')
    seen=[]
    monkeypatch.setattr(runtime,'_token_request',lambda p:seen.append(p['redirect_uri']) or {'access_token':'tok-1','refresh_token':'r','expires_in':3600})
    monkeypatch.setattr(runtime,'_me',lambda t:{'id':1,'name':'김상담','email':'','role':'agent'})
    monkeypatch.setattr(runtime,'_revoke',lambda t:None)
    c=TestClient(app,base_url='http://192.168.0.10:8080')
    r=c.get('/auth/login',follow_redirects=False);q=parse_qs(urlparse(r.headers['location']).query)
    assert q['redirect_uri']==['http://192.168.0.10:8080/auth/callback']
    assert c.get(f"/auth/callback?code=c&state={q['state'][0]}",follow_redirects=False).headers['location']=='/'
    assert seen==['http://192.168.0.10:8080/auth/callback']
    # An address that is not configured is sent to the first configured one before login starts.
    r=TestClient(app,base_url='http://127.0.0.1:8080').get('/auth/login',follow_redirects=False)
    assert r.headers['location']=='http://localhost:8080/auth/login'

def test_test_write_endpoint_guards(monkeypatch):
    from app.main import engine,store
    client=logged_in();c=store.config
    assert client.post('/api/jobs/5/test-write',headers=H2).status_code==400          # live off
    c['settings']['live_writes']=True;monkeypatch.setenv('ALLOW_LIVE_WRITES','true')
    try:
        c['settings']['live_test_tickets']=[7]
        store.state['jobs']['5']={'ticket_id':5,'status':'review'};store.state['jobs']['7']={'ticket_id':7,'status':'review'}
        assert '테스트 티켓 번호나' in client.post('/api/jobs/5/test-write',headers=H2).json()['detail']
        assert client.get('/api/state').json()['test_write_ids']==[7]
        called=[]
        monkeypatch.setattr(engine,'write',lambda cfg,tid,allow_review=False:called.append((tid,allow_review)) or store.set_job(tid,write_status='done'))
        r=client.post('/api/jobs/7/test-write',headers=H2)
        assert r.status_code==200 and r.json()['write_status']=='done' and called==[(7,True)]
        assert client.get('/api/state').json()['mode']=='실제 접수 (테스트 대상만)'
    finally:
        c['settings']['live_writes']=False;c['settings'].pop('live_test_tickets',None);store.state['jobs'].pop('7',None);store.state['jobs'].pop('5',None)
