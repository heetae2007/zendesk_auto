import copy,io
from datetime import datetime,date
import pytest
from app.domain import defaults,operating,KST,redact,validate_config,lexical_search
from app.store import Store,revision
from app.engine import Engine
from app.importer import import_xlsx

NOW=datetime(2026,11,2,10,0,tzinfo=KST)

def config():
    c=defaults();c['settings']['enabled']=True;c['settings']['ai_enabled']=True;c['settings']['mapping']={'hospital_field_id':10}
    # Catalogue: 보험 defaults to 김종오 (agent-1) with 이선주 (agent-2) as backup.
    c['settings']['parts']=[part('보험','김종오','이선주'),part('원무','김구현','신승용')]
    c['settings']['work_mappings']=[]
    c['settings']['pis_days']=[{'date':'2026-11-02','members':[{'name':'이혜경','slot':'call1','absent':False}]}]
    return c

def part(name,dev='',backup='',pis='',pis_backup=''):
    return {'name':name,'active':True,'option_value':'','developer':dev,'developer_backup':backup,'pis':pis,'pis_backup':pis_backup}

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
def ai_out(part='보험',works=None):
    return {'ticket_id':1,'hospital':'예손병원','summary':'DRG 수납 오류 확인 요청','issue_type':'BUG',
        'recommended_assignments':[{'part':part,'part_id':None,'role':'PRIMARY','responsibility':'확인',
            'supporting_ticket_ids':[],'works':list(works or [])}],
        'confidence':.95,'urgent':False,'urgent_reason':'','requires_manual_review':False,'review_reasons':[],'reason':'업무 근거'}
class A:
    def __init__(self):self.out=ai_out();self.calls=[]
    def search(self,*args):return [],'fake'
    def classify(self,*args,**kw):self.calls.append((args,kw));return __import__('copy').deepcopy(self.out)
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

def people(c,assignments,loads=None,history=()):
    from app.catalog import resolve
    rows=copy.deepcopy(assignments);reasons=resolve(c,rows,'2026-11-02',loads or {},history)
    return [[p['agent_name'] for p in r['agents']] for r in rows],reasons

def test_catalogue_primary_then_backup():
    c=config();a=[{'part':'보험','works':[]}]
    assert people(c,a)[0]==[['김종오']]
    c['agents'][0]['active']=False;assert people(c,a)[0]==[['이선주']]
    c['settings']['absences']=[{'id':'x','agent_id':'agent-2','start':'2026-11-01','end':'2026-11-03'}]
    names,reasons=people(c,a);assert names==[[]] and '배정 가능한' in reasons[0]

def test_catalogue_workload_moves_to_backup():
    assert people(config(),[{'part':'보험','works':[]}],{'agent-1':20})[0]==[['이선주']]

def test_changing_work_owner_changes_routing_immediately():
    c=config();c['settings']['work_mappings']=[{'part':'보험','work':'심사','developer':'채희태','developer_backup':'이정기','active':True}]
    a=[{'part':'보험','works':['심사']}]
    assert people(c,a)[0]==[['채희태']]
    c['settings']['work_mappings'][0]['developer']='이정기'
    assert people(c,a)[0]==[['이정기']]

def test_multiple_works_give_multiple_developers_and_missing_work_is_review():
    c=config();c['settings']['work_mappings']=[{'part':'보험','work':'심사','developer':'채희태'},{'part':'보험','work':'청구','developer':'이정기'}]
    assert people(c,[{'part':'보험','works':['심사','청구']}])[0]==[['채희태','이정기']]
    names,reasons=people(c,[{'part':'보험','works':[]}])
    assert names==[['김종오']] and '세부업무가 확인되지' in reasons[0]

HIST=[{'ticket_id':1,'parts':['보험'],'participants':['이정기']},{'ticket_id':2,'parts':['보험'],'participants':['채희태']},
    {'ticket_id':3,'parts':['보험'],'participants':['이정기','채희태']},{'ticket_id':4,'parts':['원무'],'participants':['신승용']}]

def test_past_handler_only_when_catalogue_names_nobody():
    c=config();c['settings']['work_mappings']=[{'part':'보험','work':'심사','developer':'채희태'}]
    # The catalogue wins: matching work, then the part's own developer.
    assert people(c,[{'part':'보험','works':['심사']}],history=HIST)[0]==[['채희태']]
    assert people(c,[{'part':'원무','works':[],'supporting_ticket_ids':[4]}],history=HIST)[0]==[['김구현']]
    c['settings']['parts'][0].update(developer='',developer_backup='')
    names,reasons=people(c,[{'part':'보험','works':[],'supporting_ticket_ids':[1,3]}],history=HIST)
    assert names==[['이정기']] and any('과거 처리자(이정기)' in r for r in reasons)
    # Without cited tickets, every given case of the same part counts; ties keep similarity order.
    assert people(c,[{'part':'보험','works':[],'supporting_ticket_ids':[]}],history=HIST)[0]==[['이정기']]
    # Absent people are skipped; no past handler means nobody, as before.
    c['settings']['absences']=[{'id':'x','agent_id':next(a['id'] for a in c['agents'] if a['name']=='이정기'),'start':'2026-11-01','end':'2026-11-03'}]
    assert people(c,[{'part':'보험','works':[],'supporting_ticket_ids':[1,3]}],history=HIST)[0]==[['채희태']]
    names,reasons=people(c,[{'part':'보험','works':[]}],history=[])
    assert names==[[]] and '배정 가능한' in ' '.join(reasons)

def test_excluded_names_never_resolved():
    c=config();c['settings']['parts'][0]['developer']='윤새라'
    assert people(c,[{'part':'보험','works':[]}])[0]==[['이선주']]

def test_migration_from_agent_parts_and_ignored_parts():
    from app.catalog import normalize
    c=defaults();c['agents'][0]['parts']=['보험'];c['agents'][1]['parts']=['원무'];c['agents'][2]['parts']=['원무']
    c['settings']['part_options']={'보험':'tag_ins'}
    c['settings']['work_mappings']=[{'part':'수납','work':'x'},{'part':'보험','work':'심사'}]
    c['rules']=[{'id':'r'}];normalize(c)
    p={x['name']:x for x in c['settings']['parts']}
    assert p['보험']['developer']=='김종오' and p['보험']['option_value']=='tag_ins'
    assert p['원무']['developer']==''  # two owners: order unknown, never guessed
    assert [w['part'] for w in c['settings']['work_mappings']]==['보험'] and c['rules']==[]
    assert normalize(copy.deepcopy(c))==c

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

def test_ai_disabled_requires_review(system):
    s,z,a,t,e=system;s.config['settings']['ai_enabled']=False;e.tick(NOW)
    assert s.state['jobs']['1']['status']=='review'

def test_urgent(system):
    s,z,a,t,e=system;z.t['subject']='전체 사용자 로그인 불가';e.tick(NOW)
    j=s.state['jobs']['1']
    # Still analysed (parts, people) so it can be registered; marked urgent and left for review.
    assert j['urgent'] and j['urgent_keyword'] and j['status']=='review' and j['recommended_assignments']
    assert any('긴급 의심' in r for r in j['review_reasons'])

def test_api_failure(system):
    s,z,a,t,e=system;z.fail=True;assert e.tick(NOW).get('error')
    assert not s.state['jobs']

def test_ai_failure_retries(system):
    s,z,a,t,e=system
    def fail(*a,**k):raise TimeoutError()
    a.classify=fail
    for _ in range(4):e.tick(NOW)
    assert s.state['jobs']['1']['status']=='review'

def test_pre_review_screen_one_per_part(system):
    s,z,a,t,e=system;z.t['subject']='사전심사 화면 금액 불일치'
    s.config['settings']['work_mappings']=[{'part':'보험','work':'심사','developer':'채희태'},{'part':'보험','work':'청구','developer':'이정기'}]
    a.out=ai_out(works=['심사','청구']);e.tick(NOW)
    rows={r['part']:[p['agent_name'] for p in r['agents']] for r in s.state['jobs']['1']['recommended_assignments']}
    assert rows=={'보험':['채희태'],'원무':['김구현']}

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
    s,z,a,t,e=system;other={**z.t,'id':2,'created_at':NOW.isoformat()};r=e.analyze(z.t,s.config,{},[other],NOW)
    assert r['related_ticket_ids']==[2]
    other['custom_fields']=[{'id':10,'value':'다른병원'}]
    assert e.analyze(z.t,s.config,{},[other],NOW)['related_ticket_ids']==[]

def test_no_pis_fields():
    assert all('pis' not in k.lower() for a in defaults()['agents'] for k in a)

def test_catalogue_validation():
    c=config();c['settings']['parts'].append(part('보험'))
    with pytest.raises(ValueError):validate_config(c)
    c=config();c['settings']['work_mappings']=[{'part':'없는파트','work':'x'}]
    with pytest.raises(ValueError):validate_config(c)

class FakeSheets:
    def __init__(self,config=None,fail=False):self.config=config;self.fail=fail;self.saved=[]
    def load_config(self):
        if self.fail:raise RuntimeError('offline')
        return copy.deepcopy(self.config)
    def save_config(self,c):self.config=copy.deepcopy(c);self.saved.append(c)
    def write_rows(self,*a):pass

def test_settings_survive_restart_local(tmp_path):
    s=Store(tmp_path);c=config();c['settings']['work_mappings']=[{'part':'보험','work':'심사'}]
    s.save_config(c,revision(s.config))
    again=Store(tmp_path);assert again.config==c and again.ready

def test_settings_survive_restart_sheets(tmp_path):
    sheets=FakeSheets(config());s=Store(tmp_path,sheets)
    assert s.try_refresh() and s.config==config()
    c=copy.deepcopy(s.config);c['settings']['pis_backlog_threshold']=3;s.save_config(c,revision(s.config))
    # Restart: the saved copy is shown immediately, then confirmed from Sheets.
    again=Store(tmp_path,sheets);assert again.config==c and not again.ready
    assert again.try_refresh() and again.ready and again.config==c

def test_sheets_offline_after_restart_keeps_last_saved(tmp_path):
    s=Store(tmp_path,FakeSheets(config()));s.try_refresh()
    offline=Store(tmp_path,FakeSheets(fail=True))
    assert not offline.try_refresh() and offline.config==config() and not offline.ready
    with pytest.raises(RuntimeError):offline.save_config(config(),revision(offline.config))

def test_hospital_alias_matches_history_name():
    from app.domain import hospital_name
    assert hospital_name('부산부민')=='부산부민병원' and hospital_name(' 예손병원 ')=='예손병원' and hospital_name(None)==''
    hist=[{'ticket_id':1,'completed':True,'summary':'수납 금액 오류','hospital':'부산부민병원'},
          {'ticket_id':2,'completed':True,'summary':'수납 금액 오류','hospital':'예손병원'}]
    assert lexical_search('수납 금액 오류',hist,hospital_name('부산부민'))[0]['ticket_id']==1

def test_accuracy_needs_every_recommended_developer_to_match():
    from app.analytics import category
    base={'rec_parts':['원무','진료'],'actual_parts':['원무','진료']}
    assert category({**base,'rec_devs':['김구현','김성지'],'actual_devs':['신승용','김성지']})=='person_change'
    assert category({**base,'rec_devs':['김성지'],'actual_devs':['신승용','김성지']})=='person_change'
    assert category({**base,'rec_devs':['신승용','김성지'],'actual_devs':['김성지','신승용']})=='match'
    assert category({**base,'rec_parts':['보험'],'rec_devs':[],'actual_devs':['신승용']})=='part_error'
