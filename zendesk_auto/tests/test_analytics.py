import pytest
from test_core import config
from app.store import Store
from app.analytics import Analytics

def decision(tid,part,devs,works=(),pis='이혜경',version='base-0.11-catalog'):
    return {'ticket_id':tid,'status':'recommended','pis_name':pis,'prompt_version':version,'summary':'요약',
        'recommended_assignments':[{'part':part,'works':list(works),'agents':[{'agent_name':d} for d in devs]}]}

def row(tid,parts,people,summary='문의 요약',completed=True):
    return {'ticket_id':tid,'parts':parts,'participants':people,'summary':summary,'completed':completed,'status_conflict':False,'hospital':'병원'}

@pytest.fixture
def an(tmp_path):
    s=Store(tmp_path);c=config()
    c['settings']['work_mappings']=[{'part':'보험','work':'심사','developer':'채희태','developer_backup':'이정기'}]
    next(p for p in c['settings']['parts'] if p['name']=='보험').update(developer='채희태',developer_backup='이정기')
    s.config=c;return Analytics(s)

def test_auto_comparison_categories_and_accuracy(an):
    an.add_decision(decision(1,'보험',['채희태'],['심사']))
    an.add_decision(decision(2,'보험',['채희태'],['심사']))
    an.add_decision(decision(3,'원무',['김구현']))
    an.add_decision(decision(4,'보험',['채희태'],['심사'],version='old'))
    an.add_decision(decision(1,'원무',['김구현']))  # later re-analysis must not replace the first recommendation
    counts=an.import_cases([row(1,['보험'],['채희태','이혜경']),row(2,['보험'],['이정기','노주영']),
        row(3,['보험'],['채희태'],summary='DRG 청구 오류'),row(4,['보험'],['채희태']),row(5,['보험'],['채희태']),row(6,[],['채희태'])])
    assert counts=={'match':2,'person_change':1,'part_error':1,'no_decision':2}  # 5 and 6 were never auto-recommended
    assert an.cases['1']['category']=='match' and an.cases['2']['category']=='person_change' and an.cases['3']['category']=='part_error'
    assert an.cases['2']['pis_changed'] is True and an.cases['1']['pis_changed'] is False
    s=an.summary();assert s['scored']==4 and s['part_accuracy']==75.0 and s['dev_accuracy']==50.0
    assert {v['version'] for v in s['by_version']}=={'base-0.11-catalog','old'}

def test_work_owner_suggestion_and_prompt_examples(an):
    for tid in (1,2,3,4):an.add_decision(decision(tid,'보험',['채희태'],['심사']))
    an.add_decision(decision(5,'원무',['김구현']))
    an.import_cases([row(1,['보험'],['이정기']),row(2,['보험'],['이정기']),row(3,['보험'],['이정기']),row(4,['보험'],['채희태']),row(5,['보험'],['채희태'],summary='DRG 청구 오류')])
    # 이정기 took 3 심사 tickets the AI gave 채희태, who handled 1: proposed. Two such tickets alone would not be enough.
    assert an.suggestions()==[{'part':'보험','work':'심사','current':'채희태','current_backup':'이정기','actual':'이정기','count':3,'current_count':1}]
    an.exclude(1,True)
    assert an.suggestions()==[]
    p=an.propose()
    assert p['examples']==[{'ticket_id':5,'summary':'DRG 청구 오류','incorrect_part':'원무','correct_part':'보험'}]
    an.exclude(5,True)
    with pytest.raises(ValueError):an.propose()

def test_suggestion_needs_more_tickets_than_the_current_owner(an):
    for tid in range(1,8):an.add_decision(decision(tid,'보험',['채희태'],['심사']))
    count=lambda people:[(x['actual'],x['count'],x['current_count']) for x in an.suggestions()]
    an.import_cases([row(t,['보험'],['이정기'] if t<=3 else ['채희태']) for t in range(1,7)])
    assert count(0)==[]                             # 3 vs 3: not more than the current owner
    an.import_cases([row(t,['보험'],['이정기'] if t<=4 else ['채희태']) for t in range(1,7)])
    assert count(0)==[('이정기',4,2)]
    an.import_cases([row(t,['보험'],['이정기','채희태'] if t<=4 else ['채희태']) for t in range(1,7)])
    assert count(0)==[]                             # 채희태 shared every ticket, so he handled as many as 이정기
    an.import_cases([row(t,['보험'],['이정기']) for t in range(1,4)])
    assert count(0)==[('이정기',3,0)]

def test_tickets_stay_under_the_file_they_first_appeared_in(an):
    an.import_cases([row(1,['보험'],['채희태']),row(2,['보험'],['채희태'])],'2026-11-01까지의 실제접수이력.xlsx')
    an.import_cases([row(1,['보험'],['채희태']),row(2,['보험'],['채희태']),row(3,['보험'],['채희태'])],'2026-11-03까지의 실제접수이력.xlsx')
    assert {t:c['source_file'] for t,c in an.cases.items()}=={'1':'2026-11-01까지의 실제접수이력.xlsx','2':'2026-11-01까지의 실제접수이력.xlsx','3':'2026-11-03까지의 실제접수이력.xlsx'}

def test_daily_usage_cost_by_kst_date(an):
    from app.analytics import cost
    u=lambda at,model,i,o,r=0,w=0:{'at':at,'model':model,'input_tokens':i,'output_tokens':o,'cache_read_input_tokens':r,'cache_creation_input_tokens':w}
    assert cost(u('', 'claude-sonnet-5-5',1_000_000,100_000,500_000,200_000))==pytest.approx(2.0+1.0+0.05+0.5)
    assert cost(u('', 'claude-haiku-5-5',50_000,10_000))==pytest.approx(0.005+0.005)
    assert cost(u('', 'claude-haiku-5-5',150_000,10_000))==pytest.approx(0.075+0.025)   # prompt over 100K: higher rate card
    assert cost(u('', 'unknown-model',1,1)) is None and cost({**u('', 'claude-sonnet-4-6',1,1),'input_tokens':None}) is None
    an.usage=[u('2026-11-03T15:30:00+00:00','claude-sonnet-4-6',1_000_000,0),           # 11-04 00:30 KST
        u('2026-11-03T14:59:00+00:00','claude-sonnet-4-6',0,1_000_000),
        u('2026-11-04T01:00:00+00:00','mystery',5,5),{'at':'bad'}]
    rows=an.daily_usage()
    assert [r['date'] for r in rows]==['2026-11-04','2026-11-03']
    assert rows[0]['cost']==3.0 and rows[0]['unpriced']==1 and rows[0]['requests']==2 and rows[1]['cost']==15.0

def test_incomplete_tickets_are_not_scored(an):
    an.add_decision(decision(1,'원무',['김구현']))
    an.import_cases([row(1,['보험'],['채희태'],completed=False)])
    assert an.summary()['scored']==0
    with pytest.raises(ValueError):an.propose()
