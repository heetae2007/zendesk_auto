import copy
import pytest
from test_core import config, Z, A, T, ai_out, NOW
from app.store import Store
from app.engine import Engine
from app.domain import validate_config
from app.pis import normalize_schedule, pick_backlog
from app.clients import AI, IntegrationError, WriteRejected, WriteUnknown

DAY='2026-11-02'
MAPS=[{'part':'보험','work':'심사','developer':'김종오','developer_backup':'이선주','pis':'노주영','pis_backup':'이혜경'},
      {'part':'보험','work':'청구','developer':'이선주','developer_backup':'','pis':'노주영','pis_backup':'이혜경'}]

def oncall(*rows):
    return [{'date':DAY,'members':[{'name':n,'slot':'call1','absent':ab,'share':w} for n,w,ab in rows]}]

def roster(c,*rows,extra=()):
    """rows: (name, share %, absent). The share lives on the day's on-call entry, not on the PIS roster."""
    c['settings']['pis_members']=[{'name':n,'active':True,'option_value':''} for n,_,_ in rows]+[{'name':n,'active':True,'option_value':''} for n in extra]
    c['settings']['pis_days']=oncall(*rows)

@pytest.fixture
def system(tmp_path,monkeypatch):
    monkeypatch.setenv('ZENDESK_API_TOKEN','fake-test-only')
    monkeypatch.delenv('ALLOW_LIVE_WRITES',raising=False)
    s=Store(tmp_path);s.config=config();z=Z();a=A();t=T();return s,z,a,t,Engine(s,z,a,t)

def test_realtime_weights_from_ledger_and_never_off_roster(system):
    s,z,a,t,e=system
    roster(s.config,('A',30,False),('B',30,False),('C',40,False),('이혜경',50,True),extra=('김갑진',))
    for tid in range(1,11):
        e.pick_pis(s.config,tid,{},DAY)
    counts={}
    for v in s.state['pis'].values():counts[v['name']]=counts.get(v['name'],0)+1
    assert counts=={'A':3,'B':3,'C':4}
    # Re-analysing a ticket keeps its row: no double counting.
    first=s.state['pis']['1']['name']
    assert e.pick_pis(s.config,1,{},DAY)[0]==first and len(s.state['pis'])==10

def test_blank_shares_split_equally(system):
    s,z,a,t,e=system
    roster(s.config,('A',None,False),('B',None,False))
    for tid in range(1,5):e.pick_pis(s.config,tid,{},DAY)
    assert sorted(v['name'] for v in s.state['pis'].values())==['A','A','B','B']

@pytest.mark.parametrize('shares,ok',[((None,None,None),True),((40,30,30),True),((40,30,None),False),((40,30,20),False),((0,50,50),False),((50.5,25,24.5),False)])
def test_day_shares_must_total_100(shares,ok):
    c=config();names=('A','B','C')
    c['settings']['pis_members']=[{'name':n,'active':True,'option_value':''} for n in names+('D',)]
    c['settings']['pis_days']=[{'date':DAY,'members':[{'name':n,'slot':k,'absent':False,'share':w} for n,k,w in zip(names,('call1','call2','call3'),shares)]
        +[{'name':'D','slot':'','absent':True,'share':70}]}]
    if ok:validate_config(c)
    else:
        with pytest.raises(ValueError,match='배정 비율'):validate_config(c)

def test_inactive_member_is_not_assigned(system):
    s,z,a,t,e=system
    roster(s.config,('A',100,False));s.config['settings']['pis_members'][0]['active']=False
    assert e.pick_pis(s.config,1,{},DAY)[0] is None

def test_no_confirmed_oncall_means_review(system):
    s,z,a,t,e=system;s.config['settings']['pis_days']=[];e.tick(NOW)
    j=s.state['jobs']['1'];assert j['status']=='review' and not j['pis_name'] and 'PIS' in j['reason']

def test_realtime_tick_sets_pis_and_telegram(system):
    s,z,a,t,e=system;s.config['settings']['notify']=True
    sent=[];t.send=lambda text:sent.append(text) or 1
    e.tick(NOW);j=s.state['jobs']['1']
    assert j['status']=='recommended' and j['pis_name']=='이혜경'
    assert 'PIS 이혜경 책임님' in sent[0] and '김종오 책임님' in sent[0]

def test_backlog_primary_then_backup_then_weighted_overflow(system):
    s,z,a,t,e=system
    s.config['settings']['work_mappings']=MAPS
    roster(s.config,('노주영',25,False),('이혜경',25,False),('김갑진',50,False),('강지영',None,True),extra=('전진야',))
    used=set();r={'recommended_assignments':[{'part':'보험','works':['심사']}]}
    assert e.pick_pis(s.config,1,r,DAY,used)==('노주영','밀린 티켓 묶음: 주담당')
    assert e.pick_pis(s.config,2,r,DAY,used)==('이혜경','밀린 티켓 묶음: 백업')
    # Primary and backup are used: keep distributing by weight among today's on-call (never absent/off-call).
    rest=[e.pick_pis(s.config,tid,r,DAY,used) for tid in range(3,9)]
    assert all(reason.endswith('비중 순서 배정') for _,reason in rest)
    names=[n for n,_ in rest];assert set(names)<={'노주영','이혜경','김갑진'} and names.count('김갑진')>=3
    # No work at all still gets a PIS.
    assert e.pick_pis(s.config,20,{'recommended_assignments':[{'part':'보험','works':[]}]},DAY,set())[0]

def test_backlog_skips_off_roster_primary():
    assert pick_backlog([MAPS[0]],{'이혜경'},set())==('이혜경','백업')
    assert pick_backlog([MAPS[0]],{'강지영'},set())[0] is None
    assert pick_backlog([{}],{'이혜경'},set())[0] is None

def test_backlog_uses_part_default_when_no_work(system):
    s,z,a,t,e=system;s.config['settings']['parts'][0].update(pis='김갑진')
    roster(s.config,('김갑진',50,False),('이혜경',50,False))
    assert e.pick_pis(s.config,1,{'recommended_assignments':[{'part':'보험','works':[]}]},DAY,set())==('김갑진','밀린 티켓 묶음: 주담당')

def test_backlog_threshold_in_tick(system):
    s,z,a,t,e=system
    s.config['settings'].update(work_mappings=MAPS,pis_backlog_threshold=1);roster(s.config,('노주영',1,False))
    a.out=ai_out(works=['심사']);e.tick(NOW)
    j=s.state['jobs']['1'];assert j['pis_name']=='노주영' and j['pis_reason'].startswith('밀린 티켓')

def test_only_catalogue_works_sent_and_people_from_catalogue(system):
    s,z,a,t,e=system;s.config['settings']['work_mappings']=MAPS
    s.config['settings']['parts'][1]['active']=False
    a.out=ai_out(works=['청구']);e.tick(NOW);j=s.state['jobs']['1']
    args,kw=a.calls[0]
    assert args[1]==['보험'] and {w['work'] for w in kw['work_items']}=={'심사','청구'}
    assert all(set(w)=={'part','work'} for w in kw['work_items'])  # no names are sent to Claude
    assert j['recommended_assignments'][0]['agents'][0]['agent_name']=='이선주' and j['status']=='recommended'

def test_missing_work_is_review(system):
    s,z,a,t,e=system;s.config['settings']['work_mappings']=MAPS;e.tick(NOW)
    assert '세부업무가 확인되지 않' in s.state['jobs']['1']['reason']

def test_claude_invented_work_or_part_rejected(monkeypatch):
    ai=AI(None);out=ai_out(works=['없는업무']);out['ticket_id']=0;out['hospital']='병원'
    monkeypatch.setattr(ai,'_post',lambda *a,**k:copy.deepcopy(out))
    with pytest.raises(IntegrationError,match='세부업무'):
        ai.classify('문의',['보험'],[],None,{},'병원',work_items=MAPS)
    out['recommended_assignments'][0]['works']=['심사']
    assert ai.classify('문의',['보험'],[],None,{},'병원',work_items=MAPS)['recommended_assignments'][0]['works']==['심사']
    out['recommended_assignments'][0]['part']='없는파트'
    with pytest.raises(IntegrationError,match='파트'):ai.classify('문의',['보험'],[],None,{},'병원',work_items=MAPS)

def test_schedule_normalization_requires_review():
    out={'days':[
        {'date_text':'#VALUE!','month':None,'day':None,'call1':['이혜경'],'call2':[],'call3':[],'leave':[],'notes':'','uncertain':True},
        {'date_text':'11/3','month':11,'day':3,'call1':['이혜경'],'call2':['노주영'],'call3':['홍길동'],'leave':['노주영'],'notes':'','uncertain':False}],
        'warnings':[]}
    r=normalize_schedule(out,2026,['이혜경','노주영'])['days']
    assert r[0]['date'] is None and '날짜 미확정' in r[0]['problems'] and '셀 오류 표시' in r[0]['problems']
    assert r[1]['date']=='2026-11-03' and [m['name'] for m in r[1]['members']]==['이혜경','노주영']
    assert r[1]['members'][1]['absent'] and any('홍길동' in p for p in r[1]['problems'])

def test_read_schedule_validates_shape(monkeypatch):
    ai=AI(None)
    monkeypatch.setattr(ai,'_post',lambda *a,**k:{'days':[{'date_text':'x'}],'warnings':[]})
    with pytest.raises(IntegrationError):ai.read_schedule(b'x','image/png',['이혜경'])
    with pytest.raises(IntegrationError):ai.read_schedule(b'x','image/gif',['이혜경'])

def test_config_rejects_unknown_oncall_and_duplicate_work():
    c=config();c['settings']['pis_days']=oncall(('외부인',1,False))
    with pytest.raises(ValueError):validate_config(c)
    c=config();c['settings']['work_mappings']=[MAPS[0],MAPS[0]]
    with pytest.raises(ValueError):validate_config(c)
    c=config();roster(c,('이혜경',0,False))
    with pytest.raises(ValueError):validate_config(c)
    validate_config(config())

def test_backlog_name_rules():
    m={'part':'진료','work':'CPOE','pis':'한다연/조한슬','pis_backup':'강지영'}
    assert pick_backlog([m],{'조한슬'},set())==('조한슬','주담당')
    assert pick_backlog([{**m,'pis':'조한슬(데이터쿼리)'}],{'조한슬'},set())[0] is None

def test_analysis_failure_still_gets_pis(system):
    s,z,a,t,e=system
    def fail(*args,**kw):raise TimeoutError()
    a.classify=fail
    for _ in range(4):e.tick(NOW)
    j=s.state['jobs']['1'];assert j['status']=='review' and j['pis_name']=='이혜경'

def test_claude_payload_is_lean_cached_and_per_purpose(monkeypatch):
    import json, httpx
    from app import clients
    sent=[]
    class R:
        status_code=200
        def __init__(self,body):self.body=body
        def json(self):return self.body
    out=ai_out(works=['심사']);out['ticket_id']=0;out['hospital']='병원'
    def post(url,headers,json,timeout):
        sent.append(json);return R({'stop_reason':'end_turn','content':[{'type':'thinking','thinking':''},{'type':'text','text':clients.json.dumps(out)}],'usage':{}})
    monkeypatch.setattr(httpx,'post',post)
    monkeypatch.setenv('ANTHROPIC_API_KEY','fake-test-only');monkeypatch.setenv('ANTHROPIC_MODEL','claude-sonnet-4-6')
    monkeypatch.setenv('ANTHROPIC_MODEL_TICKET','claude-haiku-5-5');
    for n in ('ANTHROPIC_MODEL_IMAGE','ANTHROPIC_MODEL_SCHEDULE'):monkeypatch.delenv(n,raising=False)
    monkeypatch.delenv('ANTHROPIC_EFFORT',raising=False)
    ai=AI(None);history=[{'ticket_id':7,'summary':'가'*2000,'parts':['보험']}]
    ai.classify('문의',['보험'],history,None,{'purpose':'ticket'},'병원',work_items=MAPS)
    p=sent[0];stable,ticket=p['messages'][0]['content']
    assert p['model']=='claude-haiku-5-5' and p['output_config']['effort']=='low'
    assert stable['cache_control']=={'type':'ephemeral'} and 'ticket' not in json.loads(stable['text'])
    assert len(json.loads(ticket['text'])['historical_cases'][0]['summary'])==400
    # Schedule reading falls back to ANTHROPIC_MODEL; 4.x models get no effort field.
    monkeypatch.setattr(ai,'_post',clients.AI._post.__get__(ai))
    try:ai.read_schedule(b'x','image/png',['A'])
    except IntegrationError:pass
    assert sent[1]['model']=='claude-sonnet-4-6' and 'effort' not in sent[1]['output_config']
