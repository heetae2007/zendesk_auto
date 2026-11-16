import copy
import pytest
from test_core import config, Z, A, T, ai_out, NOW
from app.store import Store
from app.engine import Engine
from app.clients import WriteRejected, WriteUnknown
from app.zendesk_write import build_update, match_option, preflight, expected_people, applied

DEV_FIELD=16703320898575;PART_FIELD=360016447795

def live_config():
    c=config();m=c['settings']['mapping']
    m.update(form_id='111',custom_status_id='222',part_field_id=str(PART_FIELD),developer_field_id=str(DEV_FIELD))
    c['agents'][0]['option_value']='dev_kim'
    c['settings']['pis_members']=[{'name':'이혜경','active':True,'option_value':'pis_lee'}]
    c['settings']['parts'][0]['option_value']='part_ins'
    c['settings']['live_writes']=True
    return c

def job(**kw):
    j={'ticket_id':1,'status':'recommended','source_updated_at':'2026-11-02T00:00:00Z','pis_name':'이혜경',
       'recommended_assignments':[{**ai_out()['recommended_assignments'][0],'agents':[{'agent_id':'agent-1','agent_name':'김종오','reason':'보험 기본 주담당'}]}]}
    j.update(kw);return j

TICKET={'id':1,'status':'new','updated_at':'2026-11-02T00:00:00Z','custom_fields':[{'id':DEV_FIELD,'value':None}]}

def test_payload_has_one_pis_n_devs_safe_update_and_comment():
    p=build_update(job(),live_config(),TICKET)['ticket']
    assert p['ticket_form_id']==111 and p['custom_status_id']==222
    assert {'id':DEV_FIELD,'value':['pis_lee','dev_kim']} in p['custom_fields']
    assert {'id':PART_FIELD,'value':['part_ins']} in p['custom_fields']
    assert p['safe_update'] is True and p['updated_stamp']==TICKET['updated_at']
    assert p['comment']['public'] is True and '#1이 접수' in p['comment']['body']

@pytest.mark.parametrize('change,expect',[
    (lambda j,c,t:t.update(status='open'),'신규'),
    (lambda j,c,t:j.update(status='review'),'검토'),
    (lambda j,c,t:c['settings'].update(message=''),'안내 문구'),
    (lambda j,c,t:c['settings']['mapping'].update(custom_status_id=''),'상태'),
])
def test_payload_refuses_unconfirmed_values(change,expect):
    j,c,t=job(),live_config(),copy.deepcopy(TICKET);change(j,c,t)
    with pytest.raises(ValueError,match=expect):build_update(j,c,t)

def test_option_matching_is_exact():
    f={'custom_field_options':[{'name':'PIS::이혜경','value':'a'},{'name':'채희태','value':'b'},{'name':'개발::채희태','value':'c'}]}
    assert match_option('이혜경',f)==('a','일치')
    assert match_option('채희태',f)==(None,'옵션 중복')
    assert match_option('이혜',f)==(None,'옵션 없음')

def test_preflight_reports_missing_metadata():
    c=live_config()
    meta={'forms':[{'id':111,'name':'데이터 서비스','active':True,'ticket_field_ids':[DEV_FIELD,PART_FIELD,999]}],
          'statuses':[{'id':222,'agent_label':'등록','status_category':'open','active':True}],
          'fields':[{'id':DEV_FIELD,'type':'multiselect','title':'운영팀 담당자','custom_field_options':[{'name':'이혜경','value':'pis_lee'},{'name':'김종오','value':'dev_kim'}]},
                    {'id':PART_FIELD,'type':'multiselect','title':'관련파트','custom_field_options':[{'name':'보험','value':'part_ins'}]},
                    {'id':999,'type':'text','title':'필수','required':True}]}
    r=preflight(c,meta);assert r['ok'] and r['info']['required_not_filled'][0]['id']==999
    meta['fields'][0]['type']='tagger';assert not preflight(c,meta)['ok']
    c['settings']['mapping']['form_id']='5';assert '양식 ID가' in ' '.join(preflight(c,meta)['errors'])

def test_expected_people_lists_pis_first():
    rows=expected_people(job(),live_config())
    assert [(r['role'],r['name'],r['value']) for r in rows]==[('PIS','이혜경','pis_lee'),('개발','김종오','dev_kim')]

class LiveZ(Z):
    def __init__(self,error=None):
        super().__init__();self.t['custom_fields']=[{'id':10,'value':'예손병원'}];self.updates=[];self.error=error
    def update_ticket(self,tid,payload):
        self.updates.append(payload)
        if self.error:raise self.error
        return {}

@pytest.fixture
def live(tmp_path,monkeypatch):
    monkeypatch.setenv('ZENDESK_API_TOKEN','fake-test-only');monkeypatch.setenv('ALLOW_LIVE_WRITES','true')
    def make(error=None,env=True):
        if not env:monkeypatch.setenv('ALLOW_LIVE_WRITES','false')
        s=Store(tmp_path);s.config=live_config();z=LiveZ(error);return s,z,Engine(s,z,A(),T())
    return make

def test_env_switch_required(live):
    s,z,e=live(env=False);e.tick(NOW);assert not z.updates and s.state['jobs']['1']['status']=='recommended'

def test_success_written_once(live):
    s,z,e=live();e.tick(NOW)
    assert len(z.updates)==1 and s.state['writes']['1']['status']=='done' and s.state['jobs']['1']['write_status']=='done'
    s.state['jobs']['1']['status']='queued';e.tick(NOW);assert len(z.updates)==1

def test_timeout_is_unknown_and_never_retried(live):
    s,z,e=live(WriteUnknown('timeout'));e.tick(NOW);e.tick(NOW)
    assert len(z.updates)==1 and s.state['writes']['1']['status']=='unknown' and s.state['jobs']['1']['status']=='review'
    z.t.update(status='open',custom_status_id=222,updated_at='x',custom_fields=[{'id':DEV_FIELD,'value':['dev_kim','pis_lee']},{'id':PART_FIELD,'value':['part_ins']}])
    assert e.verify_write(1)=='done'

def test_conflict_is_definite_failure(live):
    s,z,e=live(WriteRejected('409'));e.tick(NOW)
    assert s.state['writes']['1']['status']=='failed' and s.state['jobs']['1']['write_status']=='failed'

@pytest.mark.parametrize('change,note,kept',[
    (lambda j,c,t:j.update(pis_name=''),'PIS 미배정',['dev_kim']),
    (lambda j,c,t:c['agents'][0].update(option_value=''),'선택값 미설정: 김종오',['pis_lee']),
    (lambda j,c,t:j.update(recommended_assignments=[]),'개발 담당자 없음',['pis_lee']),
])
def test_missing_values_are_left_out_not_blocking(change,note,kept):
    j,c,t=job(),live_config(),copy.deepcopy(TICKET);change(j,c,t)
    p=build_update(j,c,t)
    assert any(note in n for n in p['notes'])
    assert {'id':DEV_FIELD,'value':kept} in p['ticket']['custom_fields'] and p['ticket']['custom_status_id']==222

def test_missing_part_option_is_left_out():
    c=live_config();c['settings']['parts'][0]['option_value']=''
    p=build_update(job(),c,TICKET)
    assert all(f['id']!=PART_FIELD for f in p['ticket']['custom_fields']) and any('관련파트' in n for n in p['notes'])

def test_human_value_is_kept_and_ticket_still_registered(live):
    s,z,e=live();z.t['custom_fields'].append({'id':DEV_FIELD,'value':['manual']});e.tick(NOW)
    assert len(z.updates)==1 and s.state['jobs']['1']['write_status']=='done'
    sent=z.updates[0]['ticket']
    assert all(f['id']!=DEV_FIELD for f in sent['custom_fields']) and sent['custom_status_id']==222 and 'notes' not in z.updates[0]
    assert s.state['jobs']['1']['status']=='review' and '이미 값이 있어 유지' in s.state['jobs']['1']['reason']

def test_applied_readback():
    p=build_update(job(),live_config(),TICKET)
    assert applied(TICKET,p) is False
    assert applied({**TICKET,'status':'open','updated_at':'y'},p) is None

def test_other_form_is_kept():
    p=build_update(job(),live_config(),{**TICKET,'ticket_form_id':999})
    assert 'ticket_form_id' not in p['ticket'] and any('다른 양식' in n for n in p['notes'])
    assert build_update(job(),live_config(),{**TICKET,'ticket_form_id':999},default_form_id=999)['ticket']['ticket_form_id']==111  # account default = not chosen
    assert build_update(job(),live_config(),{**TICKET,'ticket_form_id':111})['ticket']['ticket_form_id']==111

def test_engine_registers_other_form_without_changing_it(live):
    s,z,e=live();z.t['ticket_form_id']=555;z.forms=lambda:[{'id':1,'default':True}];e.tick(NOW)
    assert len(z.updates)==1 and 'ticket_form_id' not in z.updates[0]['ticket'] and '다른 양식' in s.state['jobs']['1']['reason']

def test_analysis_failure_still_registers(live):
    s,z,e=live();s.config['settings']['live_include_review']=True
    def boom(*a,**k):raise RuntimeError('설정에 없는 세부업무')
    e.ai.classify=boom
    for _ in range(4):e.tick(NOW)
    j=s.state['jobs']['1'];sent=z.updates[0]['ticket']
    assert len(z.updates)==1 and j['write_status']=='done' and j['status']=='review'
    assert sent['custom_status_id']==222 and sent['comment']['public'] and '3회 초과' in j['reason']

def test_listed_test_tickets_are_the_only_live_targets(live):
    s,z,e=live();s.config['settings']['live_test_tickets']=[999];e.tick(NOW)
    # Ticket 1 is recommended, but it is not a test ticket: nothing is written.
    assert not z.updates and s.state['jobs']['1']['status']=='recommended' and '1' not in s.state.get('writes',{})
    s.state['jobs'].pop('1');s.config['settings']['live_test_tickets']=[1];e.tick(NOW)
    assert len(z.updates)==1 and s.state['writes']['1']['status']=='done'

def test_review_is_written_only_by_operator_on_test_ticket():
    with pytest.raises(ValueError,match='검토'):build_update(job(status='review'),live_config(),TICKET)
    assert build_update(job(status='review'),live_config(),TICKET,allow_review=True)['ticket']['custom_status_id']==222
    # Every other safety check still applies.
    with pytest.raises(ValueError,match='신규'):build_update(job(status='review'),live_config(),{**TICKET,'status':'open'},allow_review=True)

def test_test_ticket_list_validated():
    from app.domain import validate_config
    for bad in ([0],['12'],[True],list(range(1,22))):
        c=live_config();c['settings']['live_test_tickets']=bad
        with pytest.raises(ValueError,match='테스트 티켓'):validate_config(c)

def test_test_hospital_matches_the_stored_tag(live):
    from app.zendesk_write import hospital_tags
    field={'custom_field_options':[{'name':'본원_1','value':'본원'},{'name':'예손병원','value':'예손병원'}]}
    assert hospital_tags(field,['본원_1'])=={'본원'} and hospital_tags(field,['없는병원'])==set()
    s,z,e=live();s.config['settings']['mapping']['hospital_field_id']='10'
    s.config['settings']['live_test_hospitals']=['본원_1']
    z.get=lambda path,params=None:{'ticket_field':field}
    e.tick(NOW)                                   # LiveZ ticket hospital is 예손병원: recommendation only
    assert not z.updates and s.state['jobs']['1']['hospital_tag']=='예손병원'
    s.state['jobs'].pop('1');z.t['custom_fields']=[{'id':10,'value':'본원'}];e._tags=(None,0,set());e.tick(NOW)
    assert len(z.updates)==1 and s.state['writes']['1']['status']=='done'

def test_test_hospital_lookup_failure_writes_nothing(live):
    s,z,e=live();s.config['settings']['mapping']['hospital_field_id']='10'
    s.config['settings']['live_test_hospitals']=['본원_1'];z.t['custom_fields']=[{'id':10,'value':'본원'}]
    def fail(path,params=None):raise RuntimeError('down')
    z.get=fail;e.tick(NOW)
    assert not z.updates and s.state['jobs']['1']['status']=='recommended'

def test_review_written_when_switch_on_but_never_urgent():
    c=live_config()
    with pytest.raises(ValueError,match='검토'):build_update(job(status='review'),c,TICKET)
    c['settings']['live_include_review']=True
    assert build_update(job(status='review'),c,TICKET)['ticket']['custom_status_id']==222
    # No developer found: registered with the PIS only.
    p=build_update(job(status='review',recommended_assignments=[{**ai_out()['recommended_assignments'][0],'agents':[]}]),c,TICKET)['ticket']
    assert {'id':DEV_FIELD,'value':['pis_lee']} in p['custom_fields']
    # Urgent tickets, by keyword or by Claude, are registered with the intake notice as well.
    assert build_update(job(status='review',urgent=True,urgent_keyword='랜섬웨어'),c,TICKET)['ticket']['comment']['public']
    assert build_update(job(status='review',urgent=True,urgent_reason='급한 요청'),c,TICKET)['ticket']['custom_status_id']==222
    assert '개발 담당자 없음' in build_update(job(recommended_assignments=[{**ai_out()['recommended_assignments'][0],'agents':[]}]),live_config(),TICKET)['notes']

def test_tick_writes_review_only_with_switch(live):
    s,z,e=live();s.config['settings']['min_confidence']=1.0;e.tick(NOW)
    assert s.state['jobs']['1']['status']=='review' and not z.updates
    s.state['jobs'].pop('1');s.config['settings']['live_include_review']=True;e.tick(NOW)
    assert s.state['jobs']['1']['status']=='review' and len(z.updates)==1 and s.state['jobs']['1']['write_status']=='done'

def test_idle_tick_skips_the_unresolved_search(live):
    s,z,e=live();calls=[];real=z.unresolved;z.unresolved=lambda:calls.append(1) or real()
    e.tick(NOW);assert len(calls)==1 and s.state['jobs']['1']['status']=='recommended'   # a ticket was waiting
    e.tick(NOW);assert len(calls)==1                                                     # nothing waiting now

def test_hospital_lookup_failure_is_remembered_briefly(live):
    s,z,e=live();s.config['settings']['mapping']['hospital_field_id']='10';s.config['settings']['live_test_hospitals']=['본원_1']
    calls=[]
    def fail(path,params=None):calls.append(path);raise RuntimeError('down')
    z.get=fail;job={'ticket_id':5,'hospital_tag':'본원'}
    assert [e.live_target(s.config,job) for _ in range(5)]==[False]*5 and len(calls)==1
