"""Usage and reviewed evaluation records, stored locally with the worker data."""
import copy, hashlib, json, threading, uuid
from .store import atomic_json,stamp
from .domain import redact

BASE_VERSION='base-0.4'

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
                'prompt_version':job.get('prompt_version',BASE_VERSION),
                'reason':redact(job.get('reason',''))[:350]})
            self._save('decision_events',self.decisions)

    def initial_decision(self,tid):
        ds=[d for d in self.decisions if str(d['ticket_id'])==str(tid) and d.get('status')=='recommended' and (d.get('agent_name') or d.get('assignments'))]
        if ds:return ds[0]
        j=self.store.state['jobs'].get(str(tid))
        if j and j.get('status')=='recommended' and j.get('agent_name'):
            return {'id':'legacy-current','at':j.get('updated_at'),'status':'recommended',
                'agent_id':j.get('agent_id'),'agent_name':j['agent_name'],'part':j.get('part',''),
                'prompt_version':'기록 없음','reason':j.get('reason','')}
        return None

    def import_cases(self,rows):
        """Preserve prior reviews only when imported candidate evidence is unchanged."""
        with self.lock:
            report={'imported':0,'no_recommendation':0,'multiple_candidates':0,'not_completed':0,'preserved_reviews':0}
            for row in rows:
                tid=str(row['ticket_id']);old=self.cases.get(tid,{})
                cand=sorted(set(row.get('participants',[])))
                d=self.initial_decision(tid)
                evidence={'candidates':cand,'parts':row.get('parts',[]),'completed':row.get('completed',False),
                    'status_conflict':row.get('status_conflict',False)}
                fingerprint=hashlib.sha256(json.dumps(evidence,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                review=copy.deepcopy(old.get('review')) if old.get('fingerprint')==fingerprint else None
                if review:report['preserved_reviews']+=1
                self.cases[tid]={'ticket_id':row['ticket_id'],'summary':redact(row.get('summary',''))[:1000],
                    'hospital':row.get('hospital',''),'candidates':cand,'parts':row.get('parts',[]),
                    'completed':row.get('completed',False),'status_conflict':row.get('status_conflict',False),
                    'source_tabs':row.get('sources',[]),'fingerprint':fingerprint,'decision':d,'review':review,
                    'imported_at':stamp()}
                report['imported']+=1
                if not d:report['no_recommendation']+=1
                if len(cand)>1:report['multiple_candidates']+=1
                if not row.get('completed'):report['not_completed']+=1
            self._save('evaluation_cases',self.cases)
            return report

    def review_case(self,tid,review):
        allowed={'match','classification_error','mapping_error','absence','workload','transfer','other'}
        if review.get('reason') not in allowed:raise ValueError('검토 사유를 선택하세요.')
        final=str(review.get('final_agent','')).strip()
        agents={a['name']:a for a in self.store.config['agents'] if a.get('name')}
        if final not in agents:raise ValueError('등록된 개발 담당자를 선택하세요.')
        with self.lock:
            case=self.cases.get(str(tid))
            if not case:raise ValueError('평가 티켓이 없습니다.')
            if not case['completed'] or case['status_conflict']:raise ValueError('완료 상태를 확인할 수 없는 티켓입니다.')
            decision=case.get('decision')
            if not decision or decision.get('status')!='recommended' or not decision.get('agent_name'):
                raise ValueError('기존 담당자 추천 이력이 없어 정확도를 평가할 수 없습니다.')
            match=decision['agent_name']==final
            part=str(review.get('correct_part','')).strip()
            if review['reason']=='classification_error' and (not part or part not in {p for a in agents.values() for p in a.get('parts',[])}):
                raise ValueError('분류 오류는 등록된 정정 파트를 입력하세요.')
            if review['reason']=='match' and not match:raise ValueError('추천 담당자와 최종 담당자가 다릅니다.')
            if match and review['reason'] not in ('match','classification_error'):
                raise ValueError('추천 담당자와 최종 담당자가 같습니다.')
            if match and review['reason']=='classification_error' and part==decision.get('part'):
                raise ValueError('분류 오류의 정정 파트는 기존 추천 파트와 달라야 합니다.')
            case['review']={'final_agent':final,'reason':review['reason'],'correct_part':part,
                'notes':redact(review.get('notes',''))[:300], 'match':match,'confirmed_at':stamp()}
            self._save('evaluation_cases',self.cases)
            return case['review']

    def summary(self):
        confirmed=[c for c in self.cases.values() if c.get('review')]
        matches=sum(c['review']['match'] for c in confirmed)
        missing=sum(1 for c in self.cases.values() if not c.get('decision'))
        reasons={}
        for c in confirmed:
            k=c['review']['reason'];reasons[k]=reasons.get(k,0)+1
        known=[u for u in self.usage if u.get('input_tokens') is not None and u.get('output_tokens') is not None]
        return {'imported':len(self.cases),'confirmed':len(confirmed),'matched':matches,
            'confirmed_match_rate':round(matches/len(confirmed)*100,1) if confirmed else None,
            'unreviewed':len(self.cases)-len(confirmed),'no_recommendation':missing,'reasons':reasons,
            'usage_requests':len(self.usage),'usage_known':len(known),
            'input_tokens':sum(u['input_tokens'] for u in known),
            'output_tokens':sum(u['output_tokens'] for u in known),
            'usage_unknown':len(self.usage)-len(known)}

    def propose(self):
        # These are data examples, not an unrestricted prompt rewrite. Safety system prompt stays intact.
        verified=[c for c in self.cases.values() if (c.get('review') or {}).get('reason')=='classification_error'
            and c['review'].get('correct_part') and c.get('summary')]
        verified=sorted(verified,key=lambda x:x['review']['confirmed_at'],reverse=True)[:8]
        if not verified:raise ValueError('PIS가 확인한 업무 분류 오류가 필요합니다.')
        examples=[{'ticket_id':c['ticket_id'],'summary':c['summary'][:350],
            'incorrect_part':c['decision'].get('part',''),'correct_part':c['review']['correct_part']} for c in verified]
        p={'id':'prompt-'+uuid.uuid4().hex[:12],'created_at':stamp(),'status':'candidate',
            'examples':examples,'method':'확인된 분류 오류를 참고 사례로 추가',
            'evaluation':'미검증: 신규 검증용 티켓으로 비교 필요'}
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
