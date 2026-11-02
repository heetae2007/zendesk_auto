"""Recommendation-only worker. Durable queue, bounded passes, no ticket mutations."""
import os, threading, copy
from datetime import datetime,timedelta
from .domain import (KST,operating,redact,rule_part,choose_agent,eligible_agents,allowed_agent,lexical_search,
    is_pre_review_screen,apply_pre_review_rule,
    urgent_candidate,similarity)
from .store import stamp,revision
from .clients import IntegrationError
from .telegram_format import format_notification

class Engine:
    def __init__(self,store,zendesk,ai,telegram,analytics=None):
        self.store=store;self.z=zendesk;self.ai=ai;self.telegram=telegram
        self.analytics=analytics
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

    def analyze(self,t,c,loads,unresolved,now,simulation=False):
        text=redact(str(t.get('subject',''))+'\n'+str(t.get('description','')))
        hospital=self.hospital(t,c)
        urgent=urgent_candidate(text)
        feedback=[{'summary':redact(f.get('summary','')),'corrected_part':f.get('part'),'reason':redact(f.get('reason',''))}
            for f in c['feedback'] if f.get('verified') and f.get('kind')=='classification_error' and similarity(text,f.get('summary',''))>.03][:5]
        examples=lexical_search(text,self.store.history,hospital)
        improvement=copy.deepcopy(c['settings'].get('prompt_improvement') or {})
        improvement['examples']=[x for x in improvement.get('examples',[]) if str(x.get('ticket_id'))!=str(t['id'])]
        prompt_version='base-0.8-pre-review'+('+'+str(improvement['id']) if improvement.get('id') else '')
        result={'hospital':hospital,'summary':text[:2000],'part':'','agent_id':None,'agent_name':'',
            'confidence':None,'reason':'','similar_tickets':examples,'search_mode':'문자 유사도 검색',
            'urgent':bool(urgent),'related_ticket_ids':[],'config_revision':revision(c),'mode':'recommendation_only','prompt_version':prompt_version}
        if urgent:result.update(status='review',reason='긴급 의심: '+urgent);return result
        rule=rule_part(c,text,hospital)
        if not c['settings'].get('ai_enabled',False):
            result.update(status='review',reason='AI 분석이 비활성화되어 있습니다. 설정을 확인하세요.');return result
        examples,search_mode=self.ai.search(text,self.store.history,hospital)
        parts=sorted({p for a in c['agents'] if allowed_agent(a) for p in a['parts']})
        by_id={}
        for part in parts:
            for a in eligible_agents(c,part,now.date().isoformat(),loads):
                mapped=bool(a.get('zendesk_id') or (a.get('option_value') and c['settings'].get('mapping',{}).get('developer_field_id')))
                count=loads.get(a['id'],0) if mapped and not simulation else None
                entry=by_id.setdefault(a['id'],{'id':a['id'],'name':a['name'],'parts':[], 'unresolved_count':count})
                entry['parts'].append(part)
        available=list(by_id.values())
        department_field=c['settings'].get('mapping',{}).get('department_field_id')
        department=next((f.get('value') for f in t.get('custom_fields',[]) if department_field and str(f.get('id'))==str(department_field)),'')
        out=self.ai.classify(text,parts,examples,feedback,rule,
            {'purpose':'simulation' if simulation else 'ticket','ticket_id':None if simulation else t['id'],'prompt_version':prompt_version,'department':redact(department)[:150]},
            improvement,available,hospital)
        assignments=out['recommended_assignments']
        # Independently revalidate the returned roster; never trust model-supplied identities.
        valid={a['id']:a for a in available}
        seen_parts=set()
        for item in assignments:
            if item.get('part') not in parts or item['part'] in seen_parts:
                raise IntegrationError('추천 파트 유효성 검증 실패')
            seen_parts.add(item['part'])
            for person in item['agents']:
                a=valid.get(person['agent_id'])
                if not a or a['name']!=person['agent_name'] or item['part'] not in a['parts'] or not allowed_agent(a):
                    raise IntegrationError('추천 담당자 유효성 검증 실패')
        pre_review_screen=is_pre_review_screen(text)
        if pre_review_screen:
            assignments=apply_pre_review_rule(assignments,available)
        reasons=list(out['review_reasons'])
        if not assignments:reasons.append('추천 파트가 없습니다.')
        if rule and rule.get('conflict'):reasons.append('같은 우선순위의 배정 규칙이 충돌합니다.')
        if rule and rule.get('part') and rule['part'] not in [a['part'] for a in assignments]:reasons.append('AI 추천과 배정 규칙이 다릅니다.')
        if not hospital:reasons.append('병원 필드 매핑이 필요합니다.')
        if any(not a['agents'] for a in assignments):reasons.append('담당자가 확인되지 않은 파트가 있습니다.')
        if any(a['role']=='UNDETERMINED' for a in assignments):reasons.append('주담당과 협업 담당 구분이 필요합니다.')
        if out['urgent']:reasons.append(out['urgent_reason'] or '긴급 문의 검토가 필요합니다.')
        if out['confidence']<c['settings']['min_confidence']:reasons.append('추천 신뢰도가 기준보다 낮습니다.')
        if out['requires_manual_review']:reasons.append('AI가 검토를 요청했습니다.')
        all_people=[person for assignment in assignments for person in assignment['agents']]
        # Preserve legacy scalar fields only when a single selection is unambiguous.
        single=len(assignments)==1 and len(all_people)==1
        result.update(recommended_assignments=assignments,issue_type=out['issue_type'],
            part=assignments[0]['part'] if len(assignments)==1 else '',
            agent_id=all_people[0]['agent_id'] if single else None,
            agent_name=all_people[0]['agent_name'] if single else '',
            summary=out['summary'],confidence=out['confidence'],reason=out['reason'],
            urgent=out['urgent'],urgent_reason=out['urgent_reason'],review_reasons=reasons,
            similar_tickets=examples,search_mode=search_mode,
            status='review' if reasons else 'recommended')
        if reasons:result['reason']=' / '.join(reasons)+' / '+out['reason']
        for other in unresolved:
            if other['id']==t['id'] or self.hospital(other,c)!=hospital:continue
            try:recent=datetime.fromisoformat(other['created_at'].replace('Z','+00:00'))>=now-timedelta(days=7)
            except (ValueError,KeyError):recent=False
            if recent and similarity(text,str(other.get('subject',''))+'\n'+str(other.get('description','')))>=.5:
                result['related_ticket_ids'].append(other['id'])
        if any(not next((a for a in c['agents'] if a['id']==p['agent_id']),{}).get('zendesk_id') and not next((a for a in c['agents'] if a['id']==p['agent_id']),{}).get('option_value') for p in all_people):
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
        text=format_notification(j,ticket_id=j['ticket_id'])
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
                    self.store.set_job(tid,status='review',reason='AI 분석 재시도 3회 초과');self.notify(c,self.store.state['jobs'][str(tid)]);continue
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
                        if self.analytics:self.analytics.add_decision(self.store.state['jobs'][str(tid)])
                    selected={p['agent_id'] for a in result.get('recommended_assignments',[]) for p in a['agents']} if result['status']=='recommended' else set()
                    for agent_id in selected:loads[agent_id]=loads.get(agent_id,0)+1
                    self.notify(c,self.store.state['jobs'][str(tid)])
                except Exception as e:
                    detail=str(e) if isinstance(e,IntegrationError) else type(e).__name__
                    self.store.set_job(tid,status='retry',attempts=j.get('attempts',0)+1,reason='분석·연동 오류: '+detail)
            self.store.mirror()
            self.store.state['last_error']=None
            return {'processed':min(10,len(pending))}
        except Exception as e:
            self.store.state['last_error']='연동 점검 필요: '+type(e).__name__;return {'error':self.store.state['last_error']}
        finally:
            self.store.state['last_tick']=stamp();self.store.save_state();self.run_lock.release()
