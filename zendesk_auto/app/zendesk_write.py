"""Zendesk intake payloads and preflight checks. No network here; callers own HTTP and state."""
import os
from .domain import allowed_agent
from .pis import members

def live_enabled(c):
    # Both the server env switch and the web setting are required.
    return os.getenv('ALLOW_LIVE_WRITES','').strip().lower()=='true' and c['settings'].get('live_writes') is True

def test_tickets(c):
    return [int(x) for x in c['settings'].get('live_test_tickets') or []]

def test_hospitals(c):
    return [str(x) for x in c['settings'].get('live_test_hospitals') or []]

def test_mode(c):
    """Test tickets or test hospitals are set: only those tickets are ever changed."""
    return bool(test_tickets(c) or test_hospitals(c))

def hospital_tags(field, names):
    """Tags of the hospital dropdown options named (or tagged) as listed. '본원_1' is shown, '본원' is stored."""
    wanted=set(names)
    return {str(o.get('value')) for o in (field or {}).get('custom_field_options') or [] if o.get('name') in wanted or o.get('value') in wanted}

def field(metadata, field_id):
    return next((f for f in (metadata or {}).get('fields',[]) if str(f.get('id'))==str(field_id)),None)

def options(f):
    return [o for o in (f or {}).get('custom_field_options',[]) if isinstance(o,dict)]

def match_option(name, f):
    """Exact option name, or exact last segment of a nested 'A::B' name. Never fuzzy."""
    name=str(name or '').strip()
    found=[o for o in options(f) if str(o.get('name','')).strip()==name or str(o.get('name','')).split('::')[-1].strip()==name]
    if not name or not found:return None,'옵션 없음'
    if len({o.get('value') for o in found})>1:return None,'옵션 중복'
    return found[0].get('value'),'일치'

def suggest(c, metadata):
    """Proposed name→option values for review. Never saved automatically."""
    m=c['settings'].get('mapping',{})
    dev=field(metadata,m.get('developer_field_id'));part=field(metadata,m.get('part_field_id'))
    def row(kind,name,current,f):
        value,status=match_option(name,f)
        return {'kind':kind,'name':name,'current':current or '','suggested':value or '','status':status}
    rows=[row('PIS',p['name'],p.get('option_value'),dev) for p in members(c['settings'])]
    rows+=[row('개발',a['name'],a.get('option_value'),dev) for a in c['agents'] if a.get('name') and allowed_agent(a)]
    rows+=[row('파트',p['name'],p.get('option_value'),part) for p in c['settings'].get('parts',[]) if p.get('active',True)]
    return rows

def preflight(c, metadata):
    m=c['settings'].get('mapping',{});errors=[];warnings=[];info={}
    form=next((f for f in metadata.get('forms',[]) if str(f.get('id'))==str(m.get('form_id'))),None)
    if not m.get('form_id'):errors.append('데이터 서비스 양식 ID 미설정')
    elif not form:errors.append('양식 ID가 Zendesk 양식 목록에 없습니다.')
    else:
        info['form']=form.get('name')
        default=next((f for f in metadata.get('forms',[]) if f.get('default')),None)
        info['default_form']=default.get('name') if default else None
        if not form.get('active',True):errors.append('양식이 비활성 상태입니다.')
    status=next((s for s in metadata.get('statuses',[]) if str(s.get('id'))==str(m.get('custom_status_id'))),None)
    if not m.get('custom_status_id'):errors.append('등록 사용자 지정 상태 ID 미설정')
    elif not status:errors.append('상태 ID가 Zendesk 사용자 지정 상태 목록에 없습니다.')
    else:
        info['status']={'agent_label':status.get('agent_label'),'status_category':status.get('status_category')}
        if not status.get('active',True):errors.append('사용자 지정 상태가 비활성입니다.')
    for key,label in (('part_field_id','관련파트'),('developer_field_id','운영팀 담당자')):
        f=field(metadata,m.get(key))
        if not f:errors.append(f'{label} 필드를 찾을 수 없습니다.');continue
        info[key]={'title':f.get('title'),'type':f.get('type')}
        if f.get('type')!='multiselect':errors.append(f'{label} 필드가 다중 선택(multiselect)이 아닙니다: {f.get("type")}')
        values={o.get('value') for o in options(f)}
        if key=='developer_field_id':
            for kind,people in (('PIS',[p for p in members(c['settings']) if p.get('active')]),('개발',[a for a in c['agents'] if a.get('active') and allowed_agent(a)])):
                for p in people:
                    if not p.get('option_value'):warnings.append(f'{kind} {p["name"]}: 선택값 미설정')
                    elif p['option_value'] not in values:errors.append(f'{kind} {p["name"]}: 선택값 {p["option_value"]}이(가) 필드 옵션에 없습니다.')
        else:
            for p in c['settings'].get('parts',[]):
                if not p.get('active',True):continue
                if not p.get('option_value'):warnings.append(f'파트 {p["name"]}: 선택값 미설정')
                elif p['option_value'] not in values:errors.append(f'파트 {p["name"]}: 선택값 {p["option_value"]}이(가) 필드 옵션에 없습니다.')
    if form:
        ids={str(x) for x in form.get('ticket_field_ids',[])}
        filled={str(m.get('part_field_id')),str(m.get('developer_field_id'))}
        required=[f for f in metadata.get('fields',[]) if str(f.get('id')) in ids and f.get('required') and str(f.get('id')) not in filled]
        info['required_not_filled']=[{'id':f.get('id'),'title':f.get('title'),'type':f.get('type')} for f in required]
        if required:warnings.append('양식의 필수 필드 중 자동 접수가 채우지 않는 항목이 있습니다. 시스템 필드인지, 기존 티켓에 이미 값이 있는지 확인하세요.')
        for key,label in (('part_field_id','관련파트'),('developer_field_id','운영팀 담당자'),('hospital_field_id','병원명')):
            if m.get(key) and str(m[key]) not in ids:warnings.append(f'{label} 필드가 데이터 서비스 양식에 포함되어 있지 않습니다.')
    return {'ok':not errors,'errors':errors,'warnings':warnings,'info':info,'live_enabled':live_enabled(c),
        'env_allows':os.getenv('ALLOW_LIVE_WRITES','').strip().lower()=='true'}

def expected_people(job, c):
    """Operations-team field: exactly one PIS plus N developers, as (label, option value or None)."""
    by_id={a['id']:a for a in c['agents']}
    pis={p['name']:p for p in members(c['settings'])}
    rows=[]
    if job.get('pis_name'):
        p=pis.get(job['pis_name'],{})
        rows.append({'role':'PIS','name':job['pis_name'],'value':p.get('option_value') or None})
    seen=set()
    for a in job.get('recommended_assignments',[]):
        for person in a.get('agents',[]):
            if person['agent_id'] in seen:continue
            seen.add(person['agent_id'])
            rows.append({'role':'개발','name':person['agent_name'],'value':(by_id.get(person['agent_id']) or {}).get('option_value') or None})
    return rows

def current_value(ticket, field_id):
    return next((f.get('value') for f in ticket.get('custom_fields',[]) if str(f.get('id'))==str(field_id)),None)

def build_update(job, c, ticket, default_form_id=None, allow_review=False):
    """Raise ValueError (→ human review) unless every value is confirmed.
    A form other than the target or the account default means someone chose it: leave the ticket alone."""
    m=c['settings'].get('mapping',{});tid=int(ticket['id'])
    for key in ('form_id','custom_status_id','part_field_id','developer_field_id'):
        if not str(m.get(key) or '').strip().isdigit():raise ValueError('Zendesk 양식·상태·필드 ID 설정이 필요합니다.')
    # Review results are written when the operator turned on '검토 필요도 접수' or pressed '테스트 접수'. Every other check still applies.
    review=job.get('status')=='review' and (allow_review or c['settings'].get('live_include_review') is True)
    if job.get('status')!='recommended' and not review:raise ValueError('검토가 필요한 추천은 자동 접수하지 않습니다.')
    if job.get('urgent'):raise ValueError('긴급 의심 티켓은 자동 접수하지 않습니다.')
    if ticket.get('status')!='new':raise ValueError('현재 신규 상태가 아닙니다.')
    if ticket.get('updated_at')!=job.get('source_updated_at'):raise ValueError('최초 감지 이후 변경된 티켓입니다.')
    form=ticket.get('ticket_form_id')
    if form not in (None,'') and str(form) not in (str(m['form_id']),str(default_form_id)):raise ValueError('다른 양식이 지정된 티켓은 변경하지 않습니다.')
    for key,label in (('part_field_id','관련파트'),('developer_field_id','운영팀 담당자')):
        if current_value(ticket,m[key]) not in (None,'',[]):raise ValueError(f'{label} 필드에 이미 값이 있어 덮어쓰지 않습니다.')
    people=expected_people(job,c)
    pis=[p for p in people if p['role']=='PIS'];devs=[p for p in people if p['role']=='개발']
    if len(pis)!=1:raise ValueError('PIS 담당자가 정확히 1명이 아닙니다.')
    # A review result may lack a developer: it is registered with the PIS alone and a person adds the developer.
    if not devs and not review:raise ValueError('개발 담당자가 없습니다.')
    missing=[p['name'] for p in people if not p['value']]
    if missing:raise ValueError('운영팀 담당자 선택값 미설정: '+', '.join(missing))
    values=[p['value'] for p in people]
    if len(set(values))!=len(values):raise ValueError('운영팀 담당자 선택값이 중복됩니다.')
    parts=[a['part'] for a in job.get('recommended_assignments',[])]
    options={p['name']:p.get('option_value') for p in c['settings'].get('parts',[])}
    part_values=[options.get(p) for p in parts]
    if not parts or not all(part_values):raise ValueError('관련파트 선택값 미설정: '+', '.join(p for p,v in zip(parts,part_values) if not v))
    body=str(c['settings'].get('message') or '').replace('[티켓번호]',str(tid)).strip()
    if not body:raise ValueError('접수 안내 문구가 비어 있습니다.')
    return {'ticket':{'ticket_form_id':int(m['form_id']),'custom_status_id':int(m['custom_status_id']),
        'custom_fields':[{'id':int(m['part_field_id']),'value':list(dict.fromkeys(part_values))},
                         {'id':int(m['developer_field_id']),'value':values}],
        'comment':{'body':body,'public':True},
        # Zendesk rejects the update with 409 if anyone changed the ticket after this stamp.
        'safe_update':True,'updated_stamp':ticket['updated_at']}}

def applied(ticket, payload):
    """True/False when the read-back is conclusive, None when it is not."""
    want=payload['ticket'];fields={f['id']:f['value'] for f in want['custom_fields']}
    if str(ticket.get('custom_status_id'))==str(want['custom_status_id']) and all(
        sorted(current_value(ticket,k) or [])==sorted(v) for k,v in fields.items()):return True
    if ticket.get('status')=='new' and ticket.get('updated_at')==want['updated_stamp']:return False
    return None
