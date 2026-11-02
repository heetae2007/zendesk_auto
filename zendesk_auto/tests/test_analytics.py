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

def test_owner_change_suggestion_and_prompt_examples(an):
    for tid in (1,2):an.add_decision(decision(tid,'보험',['채희태'],['심사']))
    an.add_decision(decision(3,'원무',['김구현']))
    an.import_cases([row(1,['보험'],['이정기']),row(2,['보험'],['이정기']),row(3,['보험'],['채희태'],summary='DRG 청구 오류')])
    assert an.suggestions()==[{'part':'보험','work':'심사','current':'채희태','current_backup':'이정기','actual':'이정기','count':2}]
    p=an.propose()
    assert p['examples']==[{'ticket_id':3,'summary':'DRG 청구 오류','incorrect_part':'원무','correct_part':'보험'}]
    an.exclude(3,True)
    with pytest.raises(ValueError):an.propose()

def test_incomplete_tickets_are_not_scored(an):
    an.add_decision(decision(1,'원무',['김구현']))
    an.import_cases([row(1,['보험'],['채희태'],completed=False)])
    assert an.summary()['scored']==0
    with pytest.raises(ValueError):an.propose()
