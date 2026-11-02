"""Part → work → developer/PIS catalogue. The single source for who handles what."""
import copy
from .domain import allowed_agent
from .pis import split_names

PEOPLE_KEYS = ('developer','developer_backup','pis','pis_backup')

def normalize(c):
    """Idempotent migration applied whenever config is loaded. Returns c (mutated)."""
    s = c['settings']
    if 'parts' not in s:
        # Carry the existing per-agent parts over; a default developer only where exactly one person had the part.
        names = sorted({p for a in c['agents'] if a.get('name') and allowed_agent(a) for p in a.get('parts',[])})
        options = s.get('part_options') or {}
        s['parts'] = []
        for n in names:
            owners = [a['name'] for a in c['agents'] if a.get('active') and allowed_agent(a) and n in a.get('parts',[])]
            s['parts'].append({'name':n,'active':True,'option_value':options.get(n,''),
                'developer':owners[0] if len(owners)==1 else '','developer_backup':'','pis':'','pis_backup':''})
    s.pop('part_options', None)
    known = {p['name'] for p in s['parts']}
    # Works under parts that are not in the catalogue (e.g. 수납/환자관리/배치) are ignored by decision.
    s['work_mappings'] = [w for w in s.get('work_mappings',[]) if w.get('part') in known]
    # Hospital/keyword rules and per-date developer rosters were removed in 0.11.
    c['rules'] = []; c['roster'] = []
    return c

def parts(c, active=True):
    return [p for p in c['settings'].get('parts',[]) if p.get('active',True) or not active]

def part(c, name):
    return next((p for p in c['settings'].get('parts',[]) if p['name']==name), None)

def works(c, part_name=None):
    allowed = {p['name'] for p in parts(c)}
    return [w for w in c['settings'].get('work_mappings',[]) if w.get('active',True) and w.get('part') in allowed
            and (part_name is None or w['part']==part_name)]

def validate(c):
    seen = set()
    for p in c['settings'].get('parts',[]):
        if not isinstance(p.get('name'),str) or not p['name'].strip() or p['name'] in seen:raise ValueError('파트 이름이 비어 있거나 중복됩니다.')
        seen.add(p['name'])
    for w in c['settings'].get('work_mappings',[]):
        if w.get('part') not in seen:raise ValueError(f"파트 목록에 없는 세부업무: {w.get('part')}/{w.get('work')}")

def unknown_people(c):
    """Names in the catalogue that are not on the developer/PIS rosters (shown as warnings)."""
    from .pis import members
    devs = {a['name'] for a in c['agents'] if a.get('name')}; pis = {m['name'] for m in members(c['settings'])}
    out = []
    for row in parts(c, active=False)+c['settings'].get('work_mappings',[]):
        label = row.get('part', row.get('name'))+('/'+row['work'] if 'work' in row else '')
        for key in PEOPLE_KEYS:
            roster = devs if key.startswith('developer') else pis
            out += [f'{label}: {n}' for n in split_names(row.get(key)) if n not in roster]
    return out

def resolve(c, assignments, day, loads):
    """Fill each assignment's agents from the catalogue. Returns review reasons."""
    absent = {x['agent_id'] for x in c['settings'].get('absences',[]) if x['start']<=day<=x['end']}
    by_name = {a['name']:a for a in c['agents'] if a.get('name') and allowed_agent(a)}
    def available(name):
        a = by_name.get(name)
        return a if a and a.get('active') and a['id'] not in absent and loads.get(a['id'],0)<a.get('max_load',20) else None
    reasons = []
    for item in assignments:
        rows = [w for w in works(c, item['part']) if w['work'] in item.get('works',[])]
        if not rows:
            if works(c, item['part']):reasons.append(f"{item['part']}: 세부업무가 확인되지 않았습니다.")
            rows = [part(c, item['part']) or {}]
        people = []
        for row in rows:
            label = item['part']+('/'+row['work'] if row.get('work') else ' 기본')
            pick = next(((a,'주담당') for n in split_names(row.get('developer')) if (a:=available(n))), None) \
                or next(((a,'백업') for n in split_names(row.get('developer_backup')) if (a:=available(n))), None)
            if not pick:reasons.append(f'{label}: 배정 가능한 개발 담당·백업이 없습니다.');continue
            a, tier = pick
            if all(p['agent_id']!=a['id'] for p in people):
                people.append({'agent_id':a['id'],'agent_name':a['name'],'reason':f'{label} {tier}'})
        item['agents'] = people
    return reasons

def pis_rows(c, assignments):
    """Ordered catalogue rows for the backlog PIS rule: chosen works, or the part default."""
    rows = []
    for item in assignments:
        chosen = [w for w in works(c, item['part']) if w['work'] in item.get('works',[])]
        rows += chosen or [part(c, item['part']) or {}]
    return rows
