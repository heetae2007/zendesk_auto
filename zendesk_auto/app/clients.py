import os, json, base64
from urllib.parse import urlparse
from .domain import redact, lexical_search
from .prompt_multi import SYSTEM_PROMPT

class IntegrationError(Exception): pass
class WriteRejected(IntegrationError):
    """Zendesk definitely did not apply the update (4xx, including safe-update 409)."""
class WriteUnknown(IntegrationError):
    """The update may or may not have been applied. Never retried automatically."""

class Zendesk:
    """Reads tickets; update_ticket is called only by the gated intake path."""
    def __init__(self):
        import httpx
        domain=os.getenv('ZENDESK_SUBDOMAIN','edgennext')
        if not domain.replace('-','').isalnum():raise ValueError('Zendesk subdomain 오류')
        self.base=f'https://{domain}.zendesk.com/api/v2'
        self.http=httpx.Client(auth=(os.getenv('ZENDESK_EMAIL','')+'/token',os.getenv('ZENDESK_API_TOKEN','')),timeout=25)

    def get(self,path,params=None):
        url=path if path.startswith('https://') else self.base+path
        if urlparse(url).netloc!=urlparse(self.base).netloc:raise IntegrationError('외부 pagination URL 거부')
        r=self.http.get(url,params=params)
        if r.status_code==429:raise IntegrationError('Zendesk 호출 제한. 다음 주기에 재시도합니다.')
        if r.status_code>=400:raise IntegrationError(f'Zendesk HTTP {r.status_code}')
        return r.json()

    def all(self,path,key,params=None,max_pages=100):
        values=[]
        for _ in range(max_pages):
            r=self.get(path,params);values+=r.get(key,[])
            path=r.get('next_page') or (r.get('links',{}).get('next') if r.get('meta',{}).get('has_more') else None)
            if not path:return values
            params=None
        raise IntegrationError('페이지 제한 초과; 일부 데이터로 계속하지 않습니다.')

    def metadata(self):
        return {'fields':self.all('/ticket_fields.json','ticket_fields'),
            'forms':self.forms(),
            'statuses':self.all('/custom_statuses.json','custom_statuses'),
            'groups':self.all('/groups.json','groups')}

    def forms(self):return self.all('/ticket_forms.json','ticket_forms')

    def new_page(self,url=None):
        return self.get(url or '/search/export.json',None if url else {'query':'status:new','filter[type]':'ticket','page[size]':100})

    def ticket(self,tid):return self.get(f'/tickets/{int(tid)}.json')['ticket']

    def update_ticket(self,tid,payload):
        import httpx
        try:r=self.http.put(f'{self.base}/tickets/{int(tid)}.json',json=payload)
        except httpx.TimeoutException:raise WriteUnknown('Zendesk 응답 시간 초과') from None
        except httpx.RequestError:raise WriteUnknown('Zendesk 연결 오류') from None
        if r.status_code==409:raise WriteRejected('다른 사용자가 먼저 티켓을 변경했습니다 (409).')
        if 400<=r.status_code<500:raise WriteRejected(f'Zendesk 변경 거부 HTTP {r.status_code}')
        if r.status_code>=500:raise WriteUnknown(f'Zendesk HTTP {r.status_code}')
        return r.json().get('ticket',{})

    def unresolved(self):
        rows=[];url=None
        for _ in range(100):
            r=self.get(url or '/search/export.json',None if url else {'query':'status<solved','filter[type]':'ticket','page[size]':100})
            rows+=r.get('results',[])
            if not r.get('meta',{}).get('has_more'):return rows
            url=r.get('links',{}).get('next')
            if not url:raise IntegrationError('미해결 티켓 pagination 오류')
        raise IntegrationError('미해결 티켓 조회 한도 초과')

class AI:
    def __init__(self,root,analytics=None):
        self.analytics=analytics

    def search(self,text,history,hospital):
        # Claude Messages has no embeddings endpoint; retain local lexical retrieval.
        return lexical_search(text,history,hospital), '문자 유사도 검색'

    def _post(self,system,content,schema,context,max_tokens=2048,timeout=45.0):
        """One structured-output Messages call; returns the parsed JSON object."""
        model=os.getenv('ANTHROPIC_MODEL','claude-sonnet-4-6').strip()
        key=os.getenv('ANTHROPIC_API_KEY','').strip()
        if not model or not key:raise IntegrationError('Claude 모델/API 키 미설정')
        import httpx
        payload={'model':model,'max_tokens':max_tokens,'system':system,
            'messages':[{'role':'user','content':content}],
            'output_config':{'format':{'type':'json_schema','schema':schema}}}
        headers={'x-api-key':key,'anthropic-version':'2023-06-01','content-type':'application/json'}
        workspace=os.getenv('ANTHROPIC_WORKSPACE_ID','').strip()
        if workspace:headers['anthropic-workspace-id']=workspace
        def record(status,usage=None):
            if not self.analytics:return
            usage=usage if isinstance(usage,dict) else {}
            def token(name):
                n=usage.get(name);return n if isinstance(n,int) and not isinstance(n,bool) and n>=0 else None
            try:self.analytics.add_usage({'model':model,'purpose':(context or {}).get('purpose','unknown'),
                'ticket_id':(context or {}).get('ticket_id'), 'prompt_version':(context or {}).get('prompt_version','base-0.4'),
                'status':status,'input_tokens':token('input_tokens'),'output_tokens':token('output_tokens'),
                'cache_read_input_tokens':token('cache_read_input_tokens'),
                'cache_creation_input_tokens':token('cache_creation_input_tokens')})
            except (OSError,ValueError):pass
        try:
            r=httpx.post('https://api.anthropic.com/v1/messages',headers=headers,json=payload,timeout=timeout)
        except httpx.RequestError:
            record('network_error')
            raise IntegrationError('Claude 연결 또는 응답 지연') from None
        if r.status_code>=400:
            record('http_'+str(r.status_code));raise IntegrationError(f'Claude API HTTP {r.status_code}')
        try:
            msg=r.json()
            if not isinstance(msg,dict):raise ValueError('invalid response')
        except (ValueError,TypeError):
            record('invalid_response')
            raise IntegrationError('Claude 응답 분석 실패') from None
        record('http_200',msg.get('usage'))
        try:
            if msg.get('stop_reason')!='end_turn':raise IntegrationError('Claude 응답 중단 또는 거부')
            blocks=[b.get('text','') for b in msg.get('content',[]) if b.get('type')=='text']
            if len(blocks)!=1:raise IntegrationError('Claude 응답 내용 누락')
            out=json.loads(blocks[0])
            if not isinstance(out,dict):raise IntegrationError('Claude 응답 구조 오류')
        except (ValueError,TypeError,KeyError):raise IntegrationError('Claude 응답 분석 실패') from None
        return out

    def read_schedule(self,image,media_type,roster_names):
        """Extract on-call rows from a schedule screenshot for human review."""
        from .pis import SCHEDULE_SCHEMA,SCHEDULE_PROMPT
        if media_type not in ('image/png','image/jpeg','image/webp'):raise IntegrationError('PNG, JPEG, WebP 이미지만 지원합니다.')
        content=[{'type':'image','source':{'type':'base64','media_type':media_type,'data':base64.standard_b64encode(image).decode()}},
            {'type':'text','text':json.dumps({'pis_roster':list(roster_names)},ensure_ascii=False)}]
        out=self._post(SCHEDULE_PROMPT,content,SCHEDULE_SCHEMA,{'purpose':'pis_schedule','prompt_version':'pis-schedule-1.0'},max_tokens=8000,timeout=120.0)
        if set(out)!={'days','warnings'} or not isinstance(out['days'],list) or not isinstance(out['warnings'],list):raise IntegrationError('배정표 응답 구조 오류')
        for d in out['days']:
            if not isinstance(d,dict) or set(d)!=set(SCHEDULE_SCHEMA['properties']['days']['items']['required']):raise IntegrationError('배정표 응답 구조 오류')
            for k in ('call1','call2','call3','leave'):
                if not isinstance(d[k],list) or not all(isinstance(x,str) for x in d[k]):raise IntegrationError('배정표 응답 구조 오류')
            for k in ('month','day'):
                if d[k] is not None and (isinstance(d[k],bool) or not isinstance(d[k],int)):raise IntegrationError('배정표 날짜 형식 오류')
        return out

    def classify(self,text,parts,examples,improvement=None,context=None,hospital="",work_items=None):
        """Summary, parts and works only. People are chosen from the catalogue by the caller."""
        nullable_string={'anyOf':[{'type':'string'},{'type':'null'}]}
        assignment_schema={'type':'object','additionalProperties':False,'properties':{
            'part':{'type':'string'},'part_id':nullable_string,
            'role':{'type':'string','enum':['PRIMARY','COLLABORATOR','UNDETERMINED']},
            'responsibility':{'type':'string'},
            'works':{'type':'array','items':{'type':'string'}},
            'supporting_ticket_ids':{'type':'array','items':{'type':'integer'}}},
            'required':['part','part_id','role','responsibility','works','supporting_ticket_ids']}
        schema={'type':'object','additionalProperties':False,'properties':{
            'ticket_id':{'type':'integer'},'hospital':{'type':'string'},'summary':{'type':'string'},
            'issue_type':{'type':'string','enum':['BUG','DATA_CHANGE','DATA_REQUEST','USAGE_QUESTION','OTHER','UNKNOWN']},
            'recommended_assignments':{'type':'array','items':assignment_schema},
            'confidence':{'type':'number'},'urgent':{'type':'boolean'},'urgent_reason':{'type':'string'},
            'requires_manual_review':{'type':'boolean'},'review_reasons':{'type':'array','items':{'type':'string'}},
            'reason':{'type':'string'}},
            'required':['ticket_id','hospital','summary','issue_type','recommended_assignments','confidence','urgent','urgent_reason','requires_manual_review','review_reasons','reason']}
        data={'ticket':{'ticket_id':(context or {}).get('ticket_id') or 0,'hospital':redact(hospital)[:150], 'request_department':redact((context or {}).get('department',''))[:150], 'text':redact(text)},
            'available_parts':parts,
            'work_items':[{'part':w['part'],'work':w['work']} for w in (work_items or [])],
            'historical_cases':[{'ticket_id':r['ticket_id'],'summary':redact(r['summary']),'parts':r['parts']} for r in examples],
            'reviewed_corrections':(improvement or {}).get('examples',[])}
        works_by_part={}
        for w in work_items or []:works_by_part.setdefault(w['part'],set()).add(w['work'])
        out=self._post(SYSTEM_PROMPT,json.dumps(data,ensure_ascii=False),schema,context)
        if set(out)!=set(schema['required']):raise IntegrationError('AI 응답 구조 오류')
        if isinstance(out['confidence'],bool) or not isinstance(out['confidence'],(int,float)) or not 0<=out['confidence']<=1:raise IntegrationError('AI 신뢰도 오류')
        if not isinstance(out['requires_manual_review'],bool) or not isinstance(out['urgent'],bool):raise IntegrationError('AI boolean 오류')
        if not isinstance(out['ticket_id'],int) or isinstance(out['ticket_id'],bool) or out['ticket_id']!=data['ticket']['ticket_id']:raise IntegrationError('티켓번호 불일치')
        if out['hospital']!=data['ticket']['hospital']:raise IntegrationError('병원 값 불일치')
        if out['issue_type'] not in ('BUG','DATA_CHANGE','DATA_REQUEST','USAGE_QUESTION','OTHER','UNKNOWN'):raise IntegrationError('문의 유형 오류')
        if not isinstance(out['summary'],str) or not out['summary'].strip() or not isinstance(out['reason'],str):raise IntegrationError('AI 요약 오류')
        if not isinstance(out['urgent_reason'],str) or not isinstance(out['review_reasons'],list) or not all(isinstance(x,str) for x in out['review_reasons']):raise IntegrationError('검토 사유 오류')
        assignments=out['recommended_assignments']
        if not isinstance(assignments,list) or len(assignments)>len(parts):raise IntegrationError('담당 파트 목록 오류')
        seen=set();valid_examples={r['ticket_id'] for r in examples}
        for item in assignments:
            if not isinstance(item,dict) or set(item)!=set(assignment_schema['required']):raise IntegrationError('파트 구조 오류')
            part=item['part']
            if not isinstance(part,str) or part not in parts or part in seen:raise IntegrationError('미등록 또는 중복 파트')
            seen.add(part)
            if item['part_id'] is not None:raise IntegrationError('확인되지 않은 파트 ID')
            if item['role'] not in ('PRIMARY','COLLABORATOR','UNDETERMINED') or not isinstance(item['responsibility'],str):raise IntegrationError('업무 역할 오류')
            if not isinstance(item['supporting_ticket_ids'],list) or any(isinstance(x,bool) or not isinstance(x,int) or x not in valid_examples for x in item['supporting_ticket_ids']):raise IntegrationError('존재하지 않는 근거 티켓')
            if not isinstance(item['works'],list) or not all(isinstance(w,str) for w in item['works']) or len(set(item['works']))!=len(item['works']):raise IntegrationError('세부업무 목록 오류')
            if any(w not in works_by_part.get(part,set()) for w in item['works']):raise IntegrationError('설정에 없는 세부업무')
            item['responsibility']=redact(item['responsibility'])[:350]
        out['summary']=redact(out['summary'])[:350];out['reason']=redact(out['reason'])[:350]
        out['urgent_reason']=redact(out['urgent_reason'])[:350];out['review_reasons']=[redact(x)[:350] for x in out['review_reasons'][:10]]
        return out

class Telegram:
    def send(self,text):
        import httpx
        token=os.getenv('TELEGRAM_BOT_TOKEN');chat=os.getenv('TELEGRAM_CHAT_ID')
        if not token or not chat:raise IntegrationError('Telegram 미설정')
        try:
            r=httpx.post(f'https://api.telegram.org/bot{token}/sendMessage',json={'chat_id':chat,'text':text,'parse_mode':'HTML','disable_web_page_preview':True},timeout=20)
        except httpx.RequestError:
            # Do not expose request URL (contains token).
            raise IntegrationError('Telegram 응답 확인 불가') from None
        if r.status_code>=400:raise IntegrationError(f'Telegram HTTP {r.status_code}')
        data=r.json()
        if not data.get('ok'):raise IntegrationError('Telegram 전송 거부')
        return data['result']['message_id']
