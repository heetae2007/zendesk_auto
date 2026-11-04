"""Worker: durable queue, bounded passes. Ticket mutations only via the gated intake in write()."""
import os, threading, copy
from datetime import datetime,timedelta
from .domain import (KST,operating,redact,lexical_search,is_pre_review_screen,apply_pre_review_rule,PRE_REVIEW_PARTS,
    urgent_candidate,similarity,hospital_name,allowed_agent)
from .catalog import parts,works,resolve,pis_rows
from .store import stamp,revision
from .clients import IntegrationError,WriteRejected,model_for
from .telegram_format import format_notification
from .pis import on_call,pick_realtime,pick_backlog
from .zendesk_write import live_enabled,build_update,applied

class Engine:
    def __init__(self,store,zendesk,ai,telegram,analytics=None):
        self.store=store;self.z=zendesk;self.ai=ai;self.telegram=telegram
        self.analytics=analytics
        self.run_lock=threading.Lock()

    def hospital(self,t,c):
        field=c['settings'].get('mapping',{}).get('hospital_field_id')
        raw=next((x['value'] for x in t.get('custom_fields',[]) if str(x['id'])==str(field)),None)
        return hospital_name(raw)

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
        examples=lexical_search(text,self.store.history,hospital)
        improvement=copy.deepcopy(c['settings'].get('prompt_improvement') or {})
        improvement['examples']=[x for x in improvement.get('examples',[]) if str(x.get('ticket_id'))!=str(t['id'])]
        # The model is part of the version so the analytics tab compares models like prompt versions.
        prompt_version='base-0.13-handler@'+model_for('ticket',c['settings'])+('+'+str(improvement['id']) if improvement.get('id') else '')
        result={'hospital':hospital,'summary':text[:2000],'part':'','agent_id':None,'agent_name':'',
            'confidence':None,'reason':'','similar_tickets':examples,'search_mode':'문자 유사도 검색',
            'urgent':bool(urgent),'related_ticket_ids':[],'config_revision':revision(c),'mode':'recommendation_only','prompt_version':prompt_version}
        if urgent:result.update(status='review',reason='긴급 의심: '+urgent);return result
        if not c['settings'].get('ai_enabled',False):
            result.update(status='review',reason='AI 분석이 비활성화되어 있습니다. 설정을 확인하세요.');return result
        examples,search_mode=self.ai.search(text,self.store.history,hospital)
        department_field=c['settings'].get('mapping',{}).get('department_field_id')
        department=next((f.get('value') for f in t.get('custom_fields',[]) if department_field and str(f.get('id'))==str(department_field)),'')
        # Claude decides parts and works only; people always come from the saved catalogue.
        context={'purpose':'simulation' if simulation else 'ticket','ticket_id':None if simulation else t['id'],'prompt_version':prompt_version,'department':redact(department)[:150],'settings':c['settings']}
        work_items=[{'part':w['part'],'work':w['work']} for w in works(c)]
        # Past handlers are limited to developers on the roster; PIS and others are not sent or used.
        devs={a['name'] for a in c['agents'] if a.get('name') and allowed_agent(a)}
        examples=[{**r,'participants':[n for n in r.get('participants',[]) if n in devs]} for r in examples]
        out=self.ai.classify(text,[p['name'] for p in parts(c)],examples,improvement,context,hospital,work_items=work_items)
        attachment_note=''
        # Attachments are read only when the text alone was not enough, and only when the switch is on.
        if not simulation and c['settings'].get('read_attachments') is True and (out['confidence']<c['settings']['min_confidence'] or out['requires_manual_review']):
            try:
                files,skipped=self.z.attachments(t['id'])
                if files:
                    version=prompt_version+'+첨부@'+model_for('ticket_attachment',c['settings'])
                    out=self.ai.classify(text,[p['name'] for p in parts(c)],examples,improvement,{**context,'purpose':'ticket_attachment','prompt_version':version},
                        hospital,work_items=work_items,attachments=files)
                    result.update(prompt_version=version,attachments_checked=len(files))
                attachment_note=f'첨부 {len(files)}개 확인'+(f" (제외: {', '.join(sorted(set(skipped)))})" if skipped else '') if files or skipped else '첨부 없음'
            except IntegrationError as e:
                attachment_note='첨부 확인 실패, 본문 기준 결과 유지: '+str(e)
        assignments=out['recommended_assignments']
        pre_review_screen=is_pre_review_screen(text)
        if pre_review_screen:assignments=apply_pre_review_rule(assignments)
        reasons=list(out['review_reasons'])
        reasons+=resolve(c,assignments,now.date().isoformat(),{} if simulation else loads,examples)
        if pre_review_screen:
            for item in assignments:
                if item['part'] in PRE_REVIEW_PARTS:item['agents']=item['agents'][:1]
        if not assignments:reasons.append('추천 파트가 없습니다.')
        if not hospital:reasons.append('병원 필드 매핑이 필요합니다.')
        if any(not a['agents'] for a in assignments):reasons.append('담당자가 확인되지 않은 파트가 있습니다.')
        if any(a['role']=='UNDETERMINED' for a in assignments):reasons.append('주담당과 협업 담당 구분이 필요합니다.')
        if out['urgent']:reasons.append(out['urgent_reason'] or '긴급 문의 검토가 필요합니다.')
        if out['confidence']<c['settings']['min_confidence']:reasons.append('추천 신뢰도가 기준보다 낮습니다.')
        if out['requires_manual_review']:reasons.append('AI가 검토를 요청했습니다.')
        reasons=list(dict.fromkeys(reasons))
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
        if attachment_note:result.update(attachment_note=attachment_note,reason=attachment_note+' / '+result['reason'])
        for other in unresolved:
            if other['id']==t['id'] or self.hospital(other,c)!=hospital:continue
            try:recent=datetime.fromisoformat(other['created_at'].replace('Z','+00:00'))>=now-timedelta(days=7)
            except (ValueError,KeyError):recent=False
            if recent and similarity(text,str(other.get('subject',''))+'\n'+str(other.get('description','')))>=.5:
                result['related_ticket_ids'].append(other['id'])
        if any(not next((a for a in c['agents'] if a['id']==p['agent_id']),{}).get('zendesk_id') and not next((a for a in c['agents'] if a['id']==p['agent_id']),{}).get('option_value') for p in all_people):
            result['mapping_warning']='담당자의 Zendesk ID/선택값이 미설정입니다.'
        return result

    def pick_pis(self,c,tid,result,day,batch=None,record=True):
        """Exactly one confirmed on-call PIS, or None. One ledger row per ticket, so no double counting."""
        ledger=self.store.state.setdefault('pis',{});key=str(tid)
        eligible={m['name'] for m in on_call(c['settings'],day)}
        prior=ledger.get(key)
        if prior and prior.get('date')==day and prior.get('name') in eligible and (batch is None or prior['name'] not in batch):
            if batch is not None:batch.add(prior['name'])
            return prior['name'],prior.get('reason','')
        if batch is not None:
            name,reason=pick_backlog(pis_rows(c,result.get('recommended_assignments',[])),eligible,batch)
            if not name:
                # Every ticket needs a PIS: once primary/backup are used up, continue by weight among today's on-call.
                name,why=pick_realtime(c['settings'],day,{k:v for k,v in ledger.items() if k!=key})
                reason=reason+' → 비중 순서 배정' if name else why
            reason='밀린 티켓 묶음: '+reason
            if name:batch.add(name)
        else:
            name,reason=pick_realtime(c['settings'],day,{k:v for k,v in ledger.items() if k!=key})
        if record:
            if name:ledger[key]={'date':day,'name':name,'reason':reason,'at':stamp()}
            else:ledger.pop(key,None)
        return name,reason

    def apply_pis(self,c,tid,result,day,batch=None,record=True):
        name,reason=self.pick_pis(c,tid,result,day,batch,record)
        result.update(pis_name=name or '',pis_reason=reason)
        if not name:
            result['status']='review';result['review_reasons']=list(result.get('review_reasons',[]))+['PIS 담당자 미배정: '+reason]
            result['reason']='PIS 담당자 미배정: '+reason+' / '+str(result.get('reason',''))
        return result

    def write(self,c,tid):
        """Single gated Zendesk intake. Each ticket is attempted at most once unless an operator resets a definite failure."""
        writes=self.store.state.setdefault('writes',{});key=str(tid)
        if key in writes:return
        j=self.store.state['jobs'][key]
        try:
            current=self.z.ticket(tid)
            payload=build_update(j,c,current,self.default_form_id(current))
        except ValueError as e:
            self.store.set_job(tid,status='review',write_status='blocked',reason='자동 접수 보류: '+str(e));return
        except Exception:
            self.store.set_job(tid,status='review',write_status='blocked',reason='자동 접수 보류: 접수 직전 티켓 재조회 실패');return
        writes[key]={'status':'pending','at':stamp(),'payload':payload};self.store.save_state()
        try:
            self.z.update_ticket(tid,payload)
            writes[key].update(status='done',done_at=stamp());self.store.set_job(tid,write_status='done')
        except WriteRejected as e:
            writes[key].update(status='failed',error=str(e))
            self.store.set_job(tid,status='review',write_status='failed',reason='자동 접수 실패(변경 없음): '+str(e))
        except Exception as e:
            # Timeouts may still have applied the update and posted the public comment. Never resend.
            detail=str(e) if isinstance(e,IntegrationError) else type(e).__name__
            writes[key].update(status='unknown',error=detail)
            self.store.set_job(tid,status='review',write_status='unknown',reason='자동 접수 결과 미확인: Zendesk에서 확인 후 결과 확인 버튼을 누르세요. '+detail)
        self.store.save_state()

    def default_form_id(self,ticket):
        # Only needed when the new ticket already carries a form other than the target one.
        target=str(self.store.config['settings'].get('mapping',{}).get('form_id') or '')
        if ticket.get('ticket_form_id') in (None,'') or str(ticket['ticket_form_id'])==target:return None
        return next((f['id'] for f in self.z.forms() if f.get('default')),None)

    def verify_write(self,tid):
        w=self.store.state.get('writes',{}).get(str(tid))
        if not w or w['status'] not in ('unknown','pending'):raise ValueError('확인할 미확정 접수 기록이 없습니다.')
        result=applied(self.z.ticket(tid),w['payload'])
        if result is True:
            w.update(status='done',verified_at=stamp());self.store.set_job(tid,write_status='done',reason='자동 접수 반영 확인')
        elif result is False:
            w.update(status='failed',verified_at=stamp());self.store.set_job(tid,write_status='failed',reason='자동 접수가 반영되지 않았음을 확인했습니다. 재분석할 수 있습니다.')
        self.store.save_state()
        return w['status']

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
            if not getattr(self.z,'ready',True):return {'skipped':'zendesk_not_connected'}
            self.scan(c)
            allowed,reason=operating(c,now)
            if not allowed:
                for j in list(self.store.state['jobs'].values()):
                    if j['status'] in ('queued','deferred'):self.store.set_job(j['ticket_id'],status='deferred',reason=reason)
                return {'skipped':reason}
            unresolved=self.z.unresolved();loads=self.loads(c,unresolved)
            pending=[j for j in self.store.state['jobs'].values() if j['status'] in ('queued','deferred','retry')]
            pending.sort(key=lambda j:(not j.get('urgent',False),j.get('created_at') or ''))
            threshold=c['settings'].get('pis_backlog_threshold',0)
            # Backlog rule only when an administrator configured a threshold; one batch = one pass (max 10).
            batch=set() if threshold and len(pending)>=threshold else None
            day=now.date().isoformat()
            for j in pending[:10]:
                tid=j['ticket_id']
                if j.get('attempts',0)>=3:
                    failed=self.apply_pis(c,tid,{'status':'review','reason':'AI 분석 재시도 3회 초과'},day,batch)
                    self.store.set_job(tid,**failed);self.notify(c,self.store.state['jobs'][str(tid)]);continue
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
                        self.apply_pis(c,tid,result,day,batch)
                        self.store.set_job(tid,**result)
                        if self.analytics:self.analytics.add_decision(self.store.state['jobs'][str(tid)])
                    selected={p['agent_id'] for a in result.get('recommended_assignments',[]) for p in a['agents']} if result['status']=='recommended' else set()
                    for agent_id in selected:loads[agent_id]=loads.get(agent_id,0)+1
                    if result['status']=='recommended' and live_enabled(c):self.write(c,tid)
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
