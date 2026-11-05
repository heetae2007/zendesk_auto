"""Usage records and automatic comparison of past auto-recommendations with what actually happened."""
import copy, threading, uuid
from datetime import datetime
from collections import Counter
from .store import atomic_json,stamp
from .domain import redact,KST
from .pis import members

BASE_VERSION='base-0.4'
# USD per million tokens (platform.claude.com pricing): input, 5-minute cache write, cache read, output.
# Haiku 5.5 charges more when the whole prompt (input + cache read + cache write) is over 100,000 tokens.
PRICES={'claude-haiku-5-5':[(100_000,(0.10,0.125,0.01,0.50)),(None,(0.50,0.625,0.05,2.50))],
    'claude-sonnet-5-5':[(None,(2.0,2.5,0.10,10.0))],'claude-sonnet-4-6':[(None,(3.0,3.75,0.30,15.0))]}

def cost(u):
    """USD for one Claude call, or None when its tokens or its model's price are not known."""
    tiers=PRICES.get(u.get('model'))
    if not tiers or u.get('input_tokens') is None or u.get('output_tokens') is None:return None
    read,write=u.get('cache_read_input_tokens') or 0,u.get('cache_creation_input_tokens') or 0
    prompt=u['input_tokens']+read+write
    rate=next(r for limit,r in tiers if limit is None or prompt<=limit)
    return (u['input_tokens']*rate[0]+write*rate[1]+read*rate[2]+u['output_tokens']*rate[3])/1_000_000

def category(case):
    """A recommendation matches only when its developers are exactly the people who handled the ticket."""
    rec_parts,actual_parts=set(case.get('rec_parts',[])),set(case.get('actual_parts',[]))
    rec_devs,actual_devs=set(case.get('rec_devs',[])),set(case.get('actual_devs',[]))
    if not actual_parts:return 'no_actual'
    if not rec_parts&actual_parts:return 'part_error'
    if actual_devs and rec_devs!=actual_devs:return 'person_change'
    return 'match'

def dev_score(case):
    """Partial credit for people: the share of the people who actually handled the ticket that the AI also named.
    A wrong part scores 0; a ticket with no recorded handler counts as fully right."""
    if case.get('category')=='part_error':return 0.0
    actual=set(case.get('actual_devs',[]))
    return len(actual&set(case.get('rec_devs',[])))/len(actual) if actual else 1.0

class Analytics:
    def __init__(self,store):
        self.store=store;self.lock=threading.RLock()
        self.usage=store.read('usage_events',[])
        self.decisions=store.read('decision_events',[])
        self.cases=store.read('evaluation_cases',{})
        # Cases imported under the older rule (one shared person was enough) are re-judged.
        for x in self.cases.values():
            if x.get('category') in ('match','part_error','person_change','no_actual') and 'rec_parts' in x:x['category']=category(x)
        self.prompts=store.read('prompt_versions',[])

    def _save(self,name,value):atomic_json(self.store.root/(name+'.json'),value)

    def add_usage(self,event):
        with self.lock:
            self.usage.append({'id':uuid.uuid4().hex,'at':stamp(),**event})
            self._save('usage_events',self.usage)

    def add_decision(self,job):
        if job.get('status') not in ('recommended','review'):return
        # Immutable timestamped recommendation snapshots, not current mutable jobs.
        with self.lock:
            self.decisions.append({'id':uuid.uuid4().hex,'at':stamp(),
                'ticket_id':int(job['ticket_id']),'status':job.get('status'),
                'agent_id':job.get('agent_id'),'agent_name':job.get('agent_name',''),
                'part':job.get('part',''),'assignments':copy.deepcopy(job.get('recommended_assignments',[])),
                'pis_name':job.get('pis_name',''),'summary':redact(job.get('summary',''))[:350],
                'prompt_version':job.get('prompt_version',BASE_VERSION),
                'reason':redact(job.get('reason',''))[:350]})
            self._save('decision_events',self.decisions)

    def initial_decision(self,tid):
        """The first automatic recommendation for the ticket: that is what people later changed."""
        return next((d for d in self.decisions if str(d['ticket_id'])==str(tid) and (d.get('assignments') or d.get('agent_name'))),None)

    @staticmethod
    def recommended(d):
        rows=d.get('assignments') or ([{'part':d.get('part',''),'agents':[{'agent_name':d['agent_name']}],'works':[]}] if d.get('agent_name') else [])
        return ({r['part'] for r in rows if r.get('part')},{p['agent_name'] for r in rows for p in r.get('agents',[])},rows)

    def import_cases(self,rows,source=''):
        """Compare each completed ticket in the uploaded sheet with its first auto-recommendation.
        A ticket stays listed under the file it first appeared in; tickets new to this upload get `source`."""
        c=self.store.config
        devs={a['name'] for a in c['agents'] if a.get('name')};pis={m['name'] for m in members(c['settings'])}
        with self.lock:
            old=self.cases;self.cases={}
            for row in rows:
                tid=str(row['ticket_id']);d=self.initial_decision(tid)
                actual_parts=set(row.get('parts',[]));actual_devs=set(row.get('participants',[]))&devs
                actual_pis=set(row.get('participants',[]))&pis
                case={'ticket_id':row['ticket_id'],'summary':redact(row.get('summary',''))[:1000],'hospital':row.get('hospital',''),
                    'completed':row.get('completed',False),'status_conflict':row.get('status_conflict',False),
                    'actual_parts':sorted(actual_parts),'actual_devs':sorted(actual_devs),'actual_pis':sorted(actual_pis),
                    'excluded':(old.get(tid) or {}).get('excluded',False),'imported_at':stamp(),
                    'source_file':(old.get(tid) or {}).get('source_file') or source,
                    'source_at':(old.get(tid) or {}).get('source_at') or stamp()}
                if not d:case.update(category='no_decision')
                else:
                    parts,people,assign=self.recommended(d)
                    case.update(decision_at=d['at'],prompt_version=d.get('prompt_version',BASE_VERSION),
                        rec_parts=sorted(parts),rec_devs=sorted(people),rec_pis=d.get('pis_name',''),
                        rec_works=[{'part':r['part'],'works':r.get('works',[]),'agents':[p['agent_name'] for p in r.get('agents',[])]} for r in assign])
                    case['category']=category(case)
                    case['pis_changed']=bool(actual_pis and d.get('pis_name') and d['pis_name'] not in actual_pis)
                self.cases[tid]=case
            self._save('evaluation_cases',self.cases)
            return dict(Counter(x['category'] for x in self.cases.values()))

    def exclude(self,tid,value):
        with self.lock:
            case=self.cases.get(str(tid))
            if not case:raise ValueError('비교 티켓이 없습니다.')
            case['excluded']=bool(value);self._save('evaluation_cases',self.cases);return case

    def scored(self):
        return [x for x in self.cases.values() if not x.get('excluded') and x['category'] in ('match','part_error','person_change') and x.get('completed') and not x.get('status_conflict')]

    def summary(self):
        scored=self.scored();by={}
        for x in scored:
            v=by.setdefault(x['prompt_version'],{'cases':0,'part_ok':0,'dev_ok':0})
            v['cases']+=1;v['part_ok']+=x['category']!='part_error';v['dev_ok']+=dev_score(x)
        rate=lambda n,d:round(n/d*100,1) if d else None
        known=[u for u in self.usage if u.get('input_tokens') is not None and u.get('output_tokens') is not None]
        return {'imported':len(self.cases),'scored':len(scored),
            'categories':dict(Counter(x['category'] for x in self.cases.values() if not x.get('excluded'))),
            'part_accuracy':rate(sum(x['category']!='part_error' for x in scored),len(scored)),
            'dev_accuracy':rate(sum(dev_score(x) for x in scored),len(scored)),
            'pis_changed':sum(1 for x in scored if x.get('pis_changed')),
            'by_version':[{'version':k,**v,'part_accuracy':rate(v['part_ok'],v['cases']),'dev_accuracy':rate(v['dev_ok'],v['cases'])} for k,v in sorted(by.items())],
            'usage_requests':len(self.usage),'usage_known':len(known),
            'input_tokens':sum(u['input_tokens'] for u in known),'output_tokens':sum(u['output_tokens'] for u in known),
            'cache_read_tokens':sum(u.get('cache_read_input_tokens') or 0 for u in known),
            'cache_write_tokens':sum(u.get('cache_creation_input_tokens') or 0 for u in known),
            'usage_unknown':len(self.usage)-len(known),'cost':round(sum(cost(u) or 0 for u in self.usage),4)}

    MIN_EVIDENCE=3

    def suggestions(self):
        """Work owner changes backed by past tickets. For each part/work, a person qualifies when
        - the auto-recommended people were not who handled it, yet this person did, in at least MIN_EVIDENCE tickets, and
        - that is more tickets than the work's current owner handled."""
        from .catalog import works,part
        c=self.store.config;replaced=Counter();owner_handled=Counter();rows={}
        def row(p,w):
            if (p,w) not in rows:rows[(p,w)]=next((x for x in works(c,p) if x['work']==w),None) if w else part(c,p)
            return rows[(p,w)]
        for x in self.scored():
            for r in x.get('rec_works',[]):
                if r['part'] not in x['actual_parts']:continue
                for w in r['works'] or ['']:
                    cur=(row(r['part'],w) or {}).get('developer')
                    if cur in x['actual_devs']:owner_handled[(r['part'],w)]+=1
                    if x['category']=='person_change':
                        for actual in x['actual_devs']:
                            if actual not in r['agents']:replaced[(r['part'],w,actual)]+=1
        out=[]
        for (p,w,actual),n in replaced.items():
            cur=row(p,w)
            if not cur or cur.get('developer')==actual:continue
            mine=owner_handled[(p,w)]
            if n>=self.MIN_EVIDENCE and n>mine:
                out.append({'part':p,'work':w,'current':cur.get('developer',''),'current_backup':cur.get('developer_backup',''),
                    'actual':actual,'count':n,'current_count':mine})
        return sorted(out,key=lambda x:(-x['count'],x['part'],x['work']))

    def daily_usage(self,days=30):
        """Claude calls, tokens and estimated USD per KST date, newest first. Calls whose tokens or price are unknown are counted apart."""
        rows={}
        for u in self.usage:
            try:d=datetime.fromisoformat(u['at']).astimezone(KST).date().isoformat()
            except (KeyError,ValueError):continue
            r=rows.setdefault(d,{'date':d,'requests':0,'input_tokens':0,'output_tokens':0,'cache_read_tokens':0,'cache_write_tokens':0,'cost':0.0,'unpriced':0,'models':{}})
            r['requests']+=1;usd=cost(u)
            if usd is None:r['unpriced']+=1;continue
            r['input_tokens']+=u['input_tokens'];r['output_tokens']+=u['output_tokens']
            r['cache_read_tokens']+=u.get('cache_read_input_tokens') or 0;r['cache_write_tokens']+=u.get('cache_creation_input_tokens') or 0
            r['cost']+=usd;r['models'][u['model']]=r['models'].get(u['model'],0)+usd
        return [{**r,'cost':round(r['cost'],4),'models':{k:round(v,4) for k,v in r['models'].items()}} for r in sorted(rows.values(),key=lambda r:r['date'],reverse=True)[:days]]

    def propose(self):
        # Data examples only; the safety system prompt stays intact.
        from .catalog import parts
        known={p['name'] for p in parts(self.store.config)}
        errors=[x for x in self.scored() if x['category']=='part_error' and x.get('summary') and set(x['actual_parts'])&known]
        errors=sorted(errors,key=lambda x:x['ticket_id'],reverse=True)[:12]
        if not errors:raise ValueError('업로드한 자료에서 파트 오분류 사례를 찾지 못했습니다.')
        examples=[{'ticket_id':x['ticket_id'],'summary':x['summary'][:350],'incorrect_part':', '.join(x['rec_parts']),
            'correct_part':', '.join(p for p in x['actual_parts'] if p in known)} for x in errors]
        p={'id':'prompt-'+uuid.uuid4().hex[:12],'created_at':stamp(),'status':'candidate',
            'examples':examples,'method':'업로드 자료의 파트 오분류 사례를 참고 사례로 추가',
            'evaluation':'적용 후 새로 쌓인 티켓을 다시 업로드해 버전별 정확도로 비교'}
        with self.lock:self.prompts.append(p);self._save('prompt_versions',self.prompts)
        return p

    def apply(self,ident,expected):
        p=next((p for p in self.prompts if p['id']==ident),None)
        if not p:raise ValueError('개선 후보를 찾을 수 없습니다.')
        c=copy.deepcopy(self.store.config)
        c['settings']['prompt_improvement']={'id':p['id'],'examples':copy.deepcopy(p['examples'])}
        self.store.save_config(c,expected)
        with self.lock:
            p['status']='applied';p['applied_at']=stamp();self._save('prompt_versions',self.prompts)
        return p
