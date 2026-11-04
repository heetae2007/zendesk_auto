import json, httpx, pytest
from test_core import config, Z, A, T, ai_out, NOW
from app.store import Store
from app.engine import Engine
from app.domain import validate_config
from app import clients
from app.clients import AI, IntegrationError

PNG=b'\x89PNG\r\n\x1a\n'+b'0'*20

@pytest.fixture
def system(tmp_path,monkeypatch):
    monkeypatch.setenv('ZENDESK_API_TOKEN','fake-test-only')
    s=Store(tmp_path);s.config=config();z=Z();a=A();t=T()
    z.files=([{'media_type':'image/png','data':PNG}],['지원하지 않는 형식']);z.asked=0
    def attachments(tid):
        z.asked+=1
        if isinstance(z.files,Exception):raise z.files
        return z.files
    z.attachments=attachments
    return s,z,a,t,Engine(s,z,a,t)

def low(a):a.out=ai_out();a.out['confidence']=0.5

def test_switch_off_never_reads_attachments(system):
    s,z,a,t,e=system;low(a);e.tick(NOW)
    assert z.asked==0 and len(a.calls)==1 and 'attachment_note' not in s.state['jobs']['1']

def test_confident_text_does_not_read_attachments(system):
    s,z,a,t,e=system;s.config['settings']['read_attachments']=True;e.tick(NOW)
    assert z.asked==0 and len(a.calls)==1

def test_unclear_text_rereads_with_attachments(system):
    s,z,a,t,e=system;s.config['settings']['read_attachments']=True;low(a);e.tick(NOW)
    j=s.state['jobs']['1']
    assert z.asked==1 and len(a.calls)==2 and a.calls[1][1]['attachments'][0]['media_type']=='image/png'
    assert a.calls[1][0][4]['purpose']=='ticket_attachment' and '+첨부@' in j['prompt_version']
    assert j['reason'].startswith('첨부 1개 확인 (제외: 지원하지 않는 형식)')

def test_attachment_failure_keeps_text_result(system):
    s,z,a,t,e=system;s.config['settings']['read_attachments']=True;low(a);z.files=IntegrationError('첨부 다운로드 HTTP 403');e.tick(NOW)
    j=s.state['jobs']['1'];assert len(a.calls)==1 and j['status']=='review' and '첨부 확인 실패' in j['reason']

def test_setting_must_be_bool():
    c=config();c['settings']['read_attachments']='yes'
    with pytest.raises(ValueError):validate_config(c)

def test_sniff_ignores_declared_type():
    assert clients.sniff(PNG)=='image/png' and clients.sniff(b'%PDF-1.7')=='application/pdf' and clients.sniff(b'<html>') is None

def test_credentials_never_sent_to_external_host(monkeypatch):
    seen=[]
    def handler(req):
        seen.append((req.url.host,req.headers.get('authorization')))
        if req.url.host=='edgennext.zendesk.com':
            if req.url.path.endswith('/comments.json'):
                return httpx.Response(200,json={'comments':[{'attachments':[
                    {'content_type':'image/png','size':len(PNG),'content_url':'https://edgennext.zendesk.com/attachments/token/x/?name=a.png'},
                    {'content_type':'application/vnd.ms-excel','size':10,'content_url':'https://edgennext.zendesk.com/x.xls'},
                    {'content_type':'image/png','size':9*1024*1024,'content_url':'https://edgennext.zendesk.com/big.png'}]}]})
            return httpx.Response(302,headers={'location':'https://files.zdusercontent.com/a.png'})
        return httpx.Response(200,content=PNG)
    transport=httpx.MockTransport(handler);real=httpx.Client
    monkeypatch.setattr(httpx,'Client',lambda **k:real(transport=transport,**k))
    z=clients.Zendesk()
    files,skipped=z.attachments(5)
    assert files==[{'media_type':'image/png','data':PNG}] and sorted(skipped)==['5MB 초과','지원하지 않는 형식']
    assert ('files.zdusercontent.com',None) in seen and all(auth for host,auth in seen if host=='edgennext.zendesk.com')

def test_classify_sends_files_without_names(monkeypatch):
    sent={}
    out=ai_out();out['ticket_id']=0;out['hospital']='병원'
    monkeypatch.setattr(AI,'_post',lambda self,system,content,schema,context,**k:sent.update(content=content,k=k) or json.loads(json.dumps(out)))
    AI(None).classify('문의',['보험'],[],None,{},'병원',work_items=[],attachments=[{'media_type':'image/png','data':PNG},{'media_type':'application/pdf','data':b'%PDF-1'}])
    types=[b['type'] for b in sent['content']]
    assert types==['text','image','document','text'] and sent['k']['timeout']==90.0
    assert json.loads(sent['content'][-1]['text'])['ticket']['attached_files']==2

def test_past_handlers_are_sent_with_similar_cases(monkeypatch):
    sent={}
    out=ai_out();out['ticket_id']=0;out['hospital']='병원'
    monkeypatch.setattr(AI,'_post',lambda self,system,content,schema,context,**k:sent.update(content=content) or json.loads(json.dumps(out)))
    AI(None).classify('문의',['보험'],[{'ticket_id':9,'summary':'청구 문의','parts':['보험'],'participants':['이정기']}],None,{},'병원',work_items=[])
    assert json.loads(sent['content'][-1]['text'])['historical_cases'][0]['handled_by']==['이정기']

def test_work_descriptions_go_to_the_cached_catalogue(monkeypatch):
    sent={}
    out=ai_out();out['ticket_id']=0;out['hospital']='병원'
    monkeypatch.setattr(AI,'_post',lambda self,system,content,schema,context,**k:sent.update(content=content) or json.loads(json.dumps(out)))
    AI(None).classify('문의',['보험'],[],None,{},'병원',work_items=[{'part':'보험','work':'심사','description':'심사조정·삭감 조회'},{'part':'보험','work':'청구'}])
    stable=json.loads(sent['content'][0]['text'])
    assert sent['content'][0]['cache_control'] and stable['work_items']==[{'part':'보험','work':'심사','description':'심사조정·삭감 조회'},{'part':'보험','work':'청구'}]
