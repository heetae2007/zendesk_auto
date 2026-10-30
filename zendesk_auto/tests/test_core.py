import copy,io,threading
from datetime import datetime,date
import pytest
from app.domain import defaults,operating,KST,choose_agent,rule_part,redact,validate_config
from app.store import Store,revision
from app.engine import Engine
from app.importer import import_xlsx

NOW=datetime(2026,11,2,10,0,tzinfo=KST)

def config():
    c=defaults();c['settings']['enabled']=True;c['settings']['mapping']={'hospital_field_id':10}
    c['agents'][0]['parts']=['보험'];c['agents'][1]['parts']=['보험']
    c['rules']=[{'id':'r1','part':'보험','hospital':'예손병원','keywords':['DRG'],'priority':10,'active':True}]
    c['roster']=[{'date':'2026-11-02','part':'보험','primary':['agent-1'],'backup':['agent-2']}]
    return c

class Z:
    def __init__(self):self.t={'id':1,'status':'new','updated_at':'2026-11-02T00:00:00Z','created_at':'2026-11-02T00:00:00Z','subject':'DRG 수납 오류','description':'','custom_fields':[{'id':10,'value':'예손병원'}]};self.fail=False;self.change=False
    def new_page(self,url=None):
        if self.fail:raise RuntimeError('offline')
        return {'results':[self.t.copy()],'meta':{'has_more':False}}
    def ticket(self,tid):
        t=self.t.copy()
        if self.change:t['updated_at']='2026-11-02T00:01:00Z'
        return t
    def unresolved(self):return [self.t.copy()]
class A:
    def search(self,*args):return [],'fake'
    def classify(self,*args):return {'part':'보험','confidence':.95,'reason':'업무 근거','urgent':False,'manual_review':False}
class T:
    def __init__(self):self.n=0;self.fail=False
    def send(self,text):
        self.n+=1
        if self.fail:raise TimeoutError()
        return 100

@pytest.fixture
def system(tmp_path,monkeypatch):
    monkeypatch.setenv('ZENDESK_API_TOKEN','fake-test-only')
    s=Store(tmp_path);s.config=config();z=Z();a=A();t=T();return s,z,a,t,Engine(s,z,a,t)

def test_hours_and_weekend():
    c=config()
    assert operating(c,NOW,[])[0]
    assert operating(c,NOW.replace(hour=9,minute=0),[])[0]
    assert not operating(c,NOW.replace(hour=18,minute=0),[])[0]
    assert not operating(c,NOW.replace(day=1),[])[0]

def test_holiday_and_override():
    c=config();assert not operating(c,NOW,{date(2026,11,2)})[0]
    c['calendar']=[{'date':'2026-11-02','closed':False}]
    assert operating(c,NOW,{date(2026,11,2)})[0]
    c['calendar'][0]['closed']=True
    assert not operating(c,NOW,[])[0]

def test_absent_backup():
    c=config();c['agents'][0]['active']=False
    assert choose_agent(c,'보험','2026-11-02',{})[0]['id']=='agent-2'

def test_no_backup():
    c=config();c['agents'][0]['active']=False;c['roster'][0]['backup']=[]
    assert choose_agent(c,'보험','2026-11-02',{})[0] is None

def test_workload_and_scope():
    c=config()
    assert choose_agent(c,'보험','2026-11-02',{'agent-1':20})[0]['id']=='agent-2'
    c['agents'][1]['parts']=[]
    assert choose_agent(c,'보험','2026-11-02',{'agent-1':20})[0] is None

def test_rule_conflict():
    c=config();c['rules'].append({**c['rules'][0],'id':'r2','part':'원무'})
    assert rule_part(c,'DRG','예손병원')['conflict']

def test_pii_redaction():
    s=redact('주민번호 900101-1234567 전화 010-1234-5678 이메일 a@example.com 환자명: 홍길동')
    assert all(x not in s for x in ['900101','1234-5678','a@example.com','홍길동'])

def test_normal_and_duplicate(system):
    s,z,a,t,e=system;e.tick(NOW);assert s.state['jobs']['1']['status']=='recommended'
    e.tick(NOW);assert len(s.state['jobs'])==1
    assert not hasattr(z,'update')

def test_holiday_backlog(system):
    s,z,a,t,e=system;e.tick(NOW.replace(day=1));assert s.state['jobs']['1']['status']=='deferred'
    e.tick(NOW);assert s.state['jobs']['1']['status']=='recommended'

def test_manual_change(system):
    s,z,a,t,e=system;z.change=True;e.tick(NOW)
    assert s.state['jobs']['1']['status']=='review'
    assert '변경' in s.state['jobs']['1']['reason']

def test_non_new_skipped(system):
    s,z,a,t,e=system;e.scan(s.config);z.t['status']='open';e.tick(NOW)
    assert s.state['jobs']['1']['status']=='skipped'

def test_no_examples_requires_review(system):
    s,z,a,t,e=system;s.config['rules']=[];e.tick(NOW)
    assert s.state['jobs']['1']['status']=='review'

def test_urgent(system):
    s,z,a,t,e=system;z.t['subject']='전체 사용자 로그인 불가';e.tick(NOW)
    assert s.state['jobs']['1']['urgent'] and s.state['jobs']['1']['status']=='review'

def test_api_failure(system):
    s,z,a,t,e=system;z.fail=True;assert e.tick(NOW).get('error')
    assert not s.state['jobs']

def test_ai_failure_retries(system):
    s,z,a,t,e=system;s.config['rules']=[];s.config['settings']['ai_enabled']=True
    def fail(*a):raise TimeoutError()
    a.classify=fail
    for _ in range(4):e.tick(NOW)
    assert s.state['jobs']['1']['status']=='review'

def test_invalid_ai_part(system):
    s,z,a,t,e=system;s.config['rules']=[];s.config['settings']['ai_enabled']=True
    a.classify=lambda *args:{'part':'없는파트','confidence':.99,'reason':'wrong','urgent':False,'manual_review':False}
    e.tick(NOW);assert s.state['jobs']['1']['status']=='review'

def test_telegram_failure_does_not_reprocess(system):
    s,z,a,t,e=system;s.config['settings']['notify']=True;t.fail=True;e.tick(NOW);e.tick(NOW)
    assert s.state['jobs']['1']['status']=='recommended';assert t.n==1
    assert s.state['notifications']['1:recommendation']['status']=='unknown'

def test_concurrent_run(system):
    s,z,a,t,e=system;e.run_lock.acquire()
    try:assert e.tick(NOW)=={'skipped':'already_running'}
    finally:e.run_lock.release()

def test_config_optimistic_lock(tmp_path):
    s=Store(tmp_path);r=revision(s.config);c=copy.deepcopy(s.config);c['settings']['start']='10:00';s.save_config(c,r)
    with pytest.raises(ValueError):s.save_config(c,r)

def test_persistence(tmp_path):
    s=Store(tmp_path);s.set_job(123,status='queued');assert Store(tmp_path).state['jobs']['123']['status']=='queued'

def test_import_deduplicates():
    import openpyxl
    w=openpyxl.Workbook();w.remove(w.active)
    headers=['티켓번호','요청병원','관련파트','개발담당자/\nPIS담당자','요청내용요약','Zendesk 실제상태']
    for n,status in [('김종오','처리완료'),('이선주','등록')]:
        s=w.create_sheet(n);s.append(headers);s.append([123,'예손병원','보험, 원무','김종오, PIS직원','DRG 문의',status])
    buf=io.BytesIO();w.save(buf);rows,report=import_xlsx(buf.getvalue())
    assert report['unique']==1 and report['extra_rows']==1
    assert not rows[0]['completed'] and rows[0]['status_conflict']
    assert rows[0]['participants']==['김종오']

def test_duplicate_detection_same_hospital(system):
    s,z,a,t,e=system;other={**z.t,'id':2};r=e.analyze(z.t,s.config,{},[other],NOW)
    assert r['related_ticket_ids']==[2]
    other['custom_fields']=[{'id':10,'value':'다른병원'}]
    assert e.analyze(z.t,s.config,{},[other],NOW)['related_ticket_ids']==[]

def test_no_pis_fields():
    assert all('pis' not in k.lower() for a in defaults()['agents'] for k in a)

def test_scope_conflict_validation():
    c=config();c['roster'].append(c['roster'][0].copy())
    with pytest.raises(ValueError):validate_config(c)
