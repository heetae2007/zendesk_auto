"""Usage records and automatic comparison of past auto-recommendations with what actually happened."""
import copy, threading, uuid
from collections import Counter
from .store import atomic_json,stamp
from .domain import redact
from .pis import members

BASE_VERSION='base-0.4'
CATEGORIES={'match':'일치','part_error':'파트 오분류','person_change':'담당자 변경','no_actual':'실제 담당 정보 없음','no_decision':'자동 추천 이력 없음'}

class Analytics:
    def __init__(self,store):
        self.store=store;self.lock=threading.RLock()
        self.usage=store.read('usage_events',[])
        self.decisions=store.read('decision_events',[])
        self.cases=store.read('evaluation_cases',{})
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

    def import_cases(self,rows):
        """Compare each completed ticket in the uploaded sheet with its first auto-recommendation."""
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
                    'excluded':(old.get(tid) or {}).get('excluded',False),'imported_at':stamp()}
                if not d:case.update(category='no_decision')
                else:
                    parts,people,assign=self.recommended(d)
                    case.update(decision_at=d['at'],prompt_version=d.get('prompt_version',BASE_VERSION),
                        rec_parts=sorted(parts),rec_devs=sorted(people),rec_pis=d.get('pis_name',''),
                        rec_works=[{'part':r['part'],'works':r.get('works',[]),'agents':[p['agent_name'] for p in r.get('agents',[])]} for r in assign])
                    if not actual_parts:case['category']='no_actual'
                    elif not parts&actual_parts:case['category']='part_error'
                    elif actual_devs and not people&actual_devs:case['category']='person_change'
                    else:case['category']='match'
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
            v['cases']+=1;v['part_ok']+=x['category']!='part_error';v['dev_ok']+=x['category']=='match'
        rate=lambda n,d:round(n/d*100,1) if d else None
        known=[u for u in self.usage if u.get('input_tokens') is not None and u.get('output_tokens') is not None]
        return {'imported':len(self.cases),'scored':len(scored),
            'categories':dict(Counter(x['category'] for x in self.cases.values() if not x.get('excluded'))),
            'part_accuracy':rate(sum(x['category']!='part_error' for x in scored),len(scored)),
            'dev_accuracy':rate(sum(x['category']=='match' for x in scored),len(scored)),
            'pis_changed':sum(1 for x in scored if x.get('pis_changed')),
            'by_version':[{'version':k,**v,'part_accuracy':rate(v['part_ok'],v['cases']),'dev_accuracy':rate(v['dev_ok'],v['cases'])} for k,v in sorted(by.items())],
            'usage_requests':len(self.usage),'usage_known':len(known),
            'input_tokens':sum(u['input_tokens'] for u in known),'output_tokens':sum(u['output_tokens'] for u in known),
            'usage_unknown':len(self.usage)-len(known)}

    def suggestions(self):
        """Catalogue owner changes seen repeatedly: same part/work, recommended person replaced by one other person."""
        from .catalog import works,part
        c=self.store.config;count=Counter()
        for x in self.scored():
            if x['category']!='person_change' or len(x['actual_devs'])!=1:continue
            actual=x['actual_devs'][0]
            for r in x.get('rec_works',[]):
                if r['part'] not in x['actual_parts']:continue
                for w in r['works'] or ['']:count[(r['part'],w,actual)]+=1
        out=[]
        for (p,w,actual),n in count.most_common(30):
            row=next((x for x in works(c,p) if x['work']==w),None) if w else part(c,p)
            if not row or row.get('developer')==actual:continue
            out.append({'part':p,'work':w,'current':row.get('developer',''),'current_backup':row.get('developer_backup',''),'actual':actual,'count':n})
        return out

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
