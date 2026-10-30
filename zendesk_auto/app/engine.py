"""Recommendation-only worker. Durable queue, bounded passes, no ticket mutations."""
import os, threading, copy
from datetime import datetime,timedelta
from .domain import (KST,operating,redact,rule_part,choose_agent,lexical_search,
    urgent_candidate,similarity)
from .store import stamp,revision

class Engine:
    def __init__(self,store,zendesk,ai,telegram):
        self.store=store;self.z=zendesk;self.ai=ai;self.telegram=telegram
        self.run_lock=threading.Lock()

    def hospital(self,t,c):
        field=c['settings'].get('mapping',{}).get('hospital_field_id')
        raw=next((x['value'] for x in t.get('custom_fields',[]) if str(x['id'])==str(field)),None)
        if raw is None:return ''
        match=next((h for h in c['hospitals'] if h['value']==raw),None)
        # Dropdown option values are not assumed to be display labels.
        return match['name'] if match else str(raw)

    def scan(self,c):
        cursor=self.store.state['scan'].get('next')
        # Bounded to five pages per minute; saved cursor resumes longer queues.
        for _ in range(5):
            page=self.z.new_page(cursor)
            for ticket in page.get('results',[]):
                if ticket.get('status')!='new':continue
                tid=ticket['id'];j=self.store.state['jobs'].get(str(tid))
                if j is None:
                    self.store.set_job(tid,status='queued',source_updated_at=ticket['updated_at'],created_at=ticket.get('created_at'),attempts=0)
            cursor=page.get('links',{}).get('next') if page.get('meta',{}).get('has_more') else None
            if page.get('meta',{}).get('has_more') and not cursor:raise ValueError('조회 커서 누락')
            self.store.state['scan']={'next':cursor,'updated_at':stamp()};self.store.save_state()
            if not cursor:break

    def analyze(self,t,c,loads,unresolved,now):
        text=redact(str(t.get('subject',''))+'\n'+str(t.get('description','')))
        hospital=self.hospital(t,c)
        urgent=urgent_candidate(text)
        feedback=[{'summary':redact(f.get('summary','')),'corrected_part':f.get('part'),'reason':redact(f.get('reason',''))}
            for f in c['feedback'] if f.get('verified') and f.get('kind')=='classification_error' and similarity(text,f.get('summary',''))>.03][:5]
        examples=lexical_search(text,self.store.history,hospital)
        result={'hospital':hospital,'summary':text[:2000],'part':'','agent_id':None,'agent_name':'',
            'confidence':None,'reason':'','similar_tickets':examples,'search_mode':'문자 유사도 검색',
            'urgent':bool(urgent),'related_ticket_ids':[],'config_revision':revision(c),'mode':'recommendation_only'}
        if urgent:result.update(status='review',reason='긴급 의심: '+urgent);return result
        rule=rule_part(c,text,hospital)
        if rule and rule.get('conflict'):result.update(status='review',reason='같은 우선순위의 배정 규칙이 충돌합니다.');return result
        if rule:
            result.update(part=rule['part'],reason='명시적 배정 규칙: '+rule['rule_id'])
        elif c['settings'].get('ai_enabled',False):
            examples,search_mode=self.ai.search(text,self.store.history,hospital)
            parts=sorted({p for a in c['agents'] for p in a['parts']})
            out=self.ai.classify(text,parts,examples,feedback)
            result.update(part=out['part'],confidence=out['confidence'],reason=out['reason'],urgent=out['urgent'],similar_tickets=examples,search_mode=search_mode)
            if out['manual_review'] or out['urgent'] or out['confidence']<c['settings']['min_confidence']:
                result['status']='review';return result
        else:
            result.update(status='review',reason='일치하는 규칙이 없습니다. AI 연결·활성화 후 재분석하세요.');return result
        if not hospital:
            result.update(status='review',reason='병원 필드 매핑이 필요합니다.');return result
        for other in unresolved:
            if other['id']==t['id'] or self.hospital(other,c)!=hospital:continue
            # Recent unresolved candidates only; never merge/close.
            try:recent=datetime.fromisoformat(other['created_at'].replace('Z','+00:00'))>=now-timedelta(days=7)
            except (ValueError,KeyError):recent=False
            if recent and similarity(text,str(other.get('subject',''))+'\n'+str(other.get('description','')))>=.5:
                result['related_ticket_ids'].append(other['id'])
        agent,why=choose_agent(c,result['part'],now.date().isoformat(),loads)
        if not agent:result.update(status='review',reason=result['reason']+' / '+why);return result
        result.update(status='recommended',agent_id=agent['id'],agent_name=agent['name'],selection_tier=why)
        if not agent.get('zendesk_id') and not agent.get('option_value'):
            result['mapping_warning']='담당자의 Zendesk ID/선택값이 미설정입니다.'
        return result

    def loads(self,c,unresolved):
        result={a['id']:0 for a in c['agents']};field=c['settings'].get('mapping',{}).get('developer_field_id')
        for t in unresolved:
            v=next((f['value'] for f in t.get('custom_fields',[]) if str(f['id'])==str(field)),None)
            for a in c['agents']:
                matches=a.get('zendesk_id') and str(t.get('assignee_id'))==str(a['zendesk_id'])
                if a.get('option_value'):
                    matches=matches or a['option_value'] in (v if isinstance(v,list) else [v])
                if matches:result[a['id']]+=1
        return result

    def notify(self,c,j):
        if not c['settings'].get('notify'):return
        key=str(j['ticket_id'])+':recommendation'
        existing=self.store.state['notifications'].get(key,{})
        if existing.get('status') in ('sent','unknown'):return
        self.store.state['notifications'][key]={'status':'unknown','at':stamp()};self.store.save_state()
        kind='긴급 검토 필요' if j.get('urgent') else ('검토 필요' if j['status']=='review' else '추천 결과')
        # Deliberately omit hospital, free text and patient data from Telegram.
        text=f"[Zendesk {kind} / 추천 전용]\n티켓: #{j['ticket_id']}\n실제 접수·배정은 변경하지 않았습니다.\nhttps://edgennext.zendesk.com/agent/tickets/{j['ticket_id']}"
        try:
            message_id=self.telegram.send(text)
            self.store.state['notifications'][key]={'status':'sent','message_id':message_id,'at':stamp()}
        except Exception:
            # Unknown send result: operator may explicitly reset after checking chat.
            self.store.state['notifications'][key]={'status':'unknown','at':stamp()}
        self.store.save_state()

    def tick(self,now=None):
        if not self.run_lock.acquire(blocking=False):return {'skipped':'already_running'}
        try:
            now=(now or datetime.now(KST)).astimezone(KST)
            with self.store.lock:
                c=copy.deepcopy(self.store.refresh())
            if not c['settings']['enabled']:return {'skipped':'disabled'}
            if not os.getenv('ZENDESK_API_TOKEN'):return {'skipped':'zendesk_not_connected'}
            self.scan(c)
            allowed,reason=operating(c,now)
            if not allowed:
                for j in list(self.store.state['jobs'].values()):
                    if j['status'] in ('queued','deferred'):self.store.set_job(j['ticket_id'],status='deferred',reason=reason)
                return {'skipped':reason}
            unresolved=self.z.unresolved();loads=self.loads(c,unresolved)
            pending=[j for j in self.store.state['jobs'].values() if j['status'] in ('queued','deferred','retry')]
            pending.sort(key=lambda j:(not j.get('urgent',False),j.get('created_at') or ''))
            for j in pending[:10]:
                tid=j['ticket_id']
                if j.get('attempts',0)>=3:
                    self.store.set_job(tid,status='review',reason='분석 재시도 3회 초과');continue
                try:
                    current=self.z.ticket(tid)
                    if current['status']!='new':self.store.set_job(tid,status='skipped',reason='현재 신규 상태가 아닙니다.');continue
                    if current['updated_at']!=j['source_updated_at']:
                        self.store.set_job(tid,status='review',reason='최초 감지 이후 변경된 티켓입니다.');continue
                    result=self.analyze(current,c,loads,unresolved,now)
                    # Settings may change while the AI call is in progress.
                    with self.store.lock:
                        fresh=self.store.refresh()
                        if revision(fresh)!=revision(c):
                            self.store.set_job(tid,status='queued',reason='설정 변경으로 다음 주기에 재분석');continue
                        self.store.set_job(tid,**result)
                    if result.get('agent_id'):loads[result['agent_id']]+=1
                    self.notify(c,self.store.state['jobs'][str(tid)])
                except Exception as e:
                    self.store.set_job(tid,status='retry',attempts=j.get('attempts',0)+1,reason='분석·연동 오류: '+type(e).__name__)
            self.store.mirror()
            self.store.state['last_error']=None
            return {'processed':min(10,len(pending))}
        except Exception as e:
            self.store.state['last_error']='연동 점검 필요: '+type(e).__name__;return {'error':self.store.state['last_error']}
        finally:
            self.store.state['last_tick']=stamp();self.store.save_state();self.run_lock.release()
