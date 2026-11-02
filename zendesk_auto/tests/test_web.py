import os
os.environ['ADMIN_PASSWORD']='test-password-not-for-production'
os.environ['DATA_DIR']='/tmp/zendesk-web-tests'
# Never touch the real Sheets store or allow live writes from tests, whatever .env says.
os.environ['STORE_MODE']='local'
os.environ['ALLOW_LIVE_WRITES']='false'
from fastapi.testclient import TestClient
from app.main import app

def test_auth_and_csrf():
    client=TestClient(app)
    assert client.get('/api/state').status_code==401
    auth=('admin',os.environ['ADMIN_PASSWORD'])
    r=client.get('/api/state',auth=auth);assert r.status_code==200
    assert r.json()['mode']=='추천 전용'
    assert client.post('/api/run',auth=auth,json={}).status_code==403
    assert client.get('/',auth=auth).status_code==200
    assert client.get('/static/app.js').status_code==200

def test_live_update_endpoint_absent():
    paths={r.path for r in app.routes}
    assert '/api/assign' not in paths and '/api/live' not in paths

H={'X-Requested-With':'zendesk-admin'}
AUTH=('admin',os.environ['ADMIN_PASSWORD'])

def test_schedule_read_returns_review_rows_only(monkeypatch):
    from app.main import engine,store
    before=store.config.get('settings',{}).get('pis_days')
    monkeypatch.setattr(engine.ai,'read_schedule',lambda data,kind,names:{'days':[
        {'date_text':'#VALUE!','month':None,'day':None,'call1':['이혜경'],'call2':[],'call3':[],'leave':[],'notes':'','uncertain':True}],'warnings':[]})
    client=TestClient(app)
    png=b'\x89PNG\r\n\x1a\n'+b'0'*20
    r=client.post('/api/pis/schedule/read?year=2026',files={'file':('a.png',png,'image/png')},auth=AUTH,headers=H)
    assert r.status_code==200 and r.json()['days'][0]['date'] is None
    assert store.config['settings'].get('pis_days')==before
    r=client.post('/api/pis/schedule/read',files={'file':('a.gif',b'GIF89a','image/gif')},auth=AUTH,headers=H)
    assert r.status_code==400

def test_state_exposes_pis_and_live_mode():
    r=TestClient(app).get('/api/state',auth=AUTH).json()
    assert r['live']=={'enabled':False,'env_allows':False,'setting':False}
    assert 'on_call' in r['pis_today'] and len(r['pis_members'])>=13

def test_config_save_validates_oncall(monkeypatch):
    from app.main import store
    from app.store import revision
    import copy
    client=TestClient(app);c=copy.deepcopy(store.config)
    c['settings']['pis_days']=[{'date':'2099-01-01','members':[{'name':'외부인','slot':'call1','weight':1,'absent':False}]}]
    assert client.post('/api/config',json={'config':c,'revision':revision(store.config)},auth=AUTH,headers=H).status_code==409
    c['settings']['pis_days'][0]['members'][0]['name']='이혜경'
    c['settings']['pis_days'].append({'date':'2000-01-01','members':[{'name':'이혜경','slot':'call1','weight':1,'absent':False}]})
    assert client.post('/api/config',json={'config':c,'revision':revision(store.config)},auth=AUTH,headers=H).status_code==200
    assert [d['date'] for d in store.config['settings']['pis_days']]==['2099-01-01']

def test_retry_refuses_after_uncertain_write():
    from app.main import store
    store.set_job(77,status='review');store.state.setdefault('writes',{})['77']={'status':'unknown'}
    r=TestClient(app).post('/api/jobs/77/retry',json={},auth=AUTH,headers=H)
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
    r=TestClient(app).post('/api/work-mapping/import',files={'file':('m.xlsx',buf.getvalue(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')},auth=AUTH,headers=H)
    assert r.status_code==200 and [x['work'] for x in r.json()['rows']]==['심사'] and r.json()['ignored']==['수납']

def test_removed_endpoints_are_gone():
    paths={r.path for r in app.routes}
    assert not {'/api/import','/api/analytics/review/{tid}','/api/pis/preview','/api/pis/backlog-preview'}&paths
