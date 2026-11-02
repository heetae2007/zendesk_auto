"""Pure routing policy. No network or Zendesk writes in this module."""
import re, unicodedata, json
from datetime import datetime, date, time
from zoneinfo import ZoneInfo
from collections import Counter

KST = ZoneInfo('Asia/Seoul')
EXCLUDED_AGENT_NAMES = frozenset({'윤세라','윤새라'})

def allowed_agent(agent):
    name=unicodedata.normalize('NFC',''.join(str(agent.get('name','')).split()))
    return bool(name) and name not in EXCLUDED_AGENT_NAMES
NAMES = ['김종오', '이선주', '김희재', '김성지', '김정진', '오형진', '박지원', '윤새라', '김구현', '이정기', '채희태', '신승용']

def defaults():
    return {
        'settings': {'enabled': False, 'start': '09:00', 'end': '18:00',
            'exclude_weekends': True, 'exclude_holidays': True, 'notify': False, 'ai_enabled': False,
            'min_confidence': .90, 'holiday_country': 'KR', 'mapping': {},
            'message': '안녕하세요. 문의하신 #[티켓번호]이 접수 되었습니다.\n\n담당 파트에 확인 후 회신 드리겠습니다.\n\n감사합니다.'},
        'agents': [{'id': f'agent-{i+1}', 'name': n, 'active': n not in EXCLUDED_AGENT_NAMES,
            'zendesk_id': None, 'option_value': '', 'max_load': 20} for i,n in enumerate(NAMES)],
        'rules': [], 'roster': [], 'calendar': [], 'feedback': [], 'hospitals': [],
    }

def redact(text):
    """Conservative baseline; not a guarantee of complete de-identification."""
    text = str(text or '')
    text = re.sub(r'\b\d{6}\s*[- ]?\s*[1-8]\d{6}\b', '[주민번호 제거]', text)
    text = re.sub(r'\b(?:01[016789]|0\d{1,2})[- .]?\d{3,4}[- .]?\d{4}\b', '[전화번호 제거]', text)
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[이메일 제거]', text)
    text = re.sub(r'((?:환자명|환자이름|성명|환자번호|등록번호|차트번호)\s*[:：=]?\s*)[^\s,;/]+', r'\1[제거]', text)
    return text[:12000]

def operating(config, now=None, holiday_dates=None):
    now = (now or datetime.now(KST)).astimezone(KST)
    s = config['settings']; d = now.date().isoformat()
    exception = next((x for x in config['calendar'] if x['date'] == d), None)
    if exception and exception['closed']:
        return False, exception.get('reason') or '운영 제외일'
    if not (exception and not exception['closed']):
        if s['exclude_weekends'] and now.weekday() >= 5:
            return False, '주말'
        if s['exclude_holidays']:
            if holiday_dates is None:
                try:
                    import holidays
                    holiday_dates = holidays.KR(years=now.year)
                except ImportError:
                    return False, '공휴일 라이브러리 미설치'
            if now.date() in holiday_dates:
                return False, '공휴일'
    start = time.fromisoformat(s['start']); end = time.fromisoformat(s['end'])
    if not start <= now.time().replace(tzinfo=None) < end:
        return False, '운영 시간 외'
    return True, '운영 시간'

def validate_config(c):
    required = set(defaults())
    if set(c) != required:
        raise ValueError('설정 항목이 올바르지 않습니다.')
    s = c['settings']
    if not time.fromisoformat(s['start']) < time.fromisoformat(s['end']):
        raise ValueError('시작 시간은 종료 시간보다 빨라야 합니다.')
    if not 0 <= float(s['min_confidence']) <= 1:
        raise ValueError('신뢰도는 0~1 범위입니다.')
    for table in ('agents','rules','feedback','hospitals'):
        ids = [x['id'] for x in c[table]]
        if len(ids) != len(set(ids)): raise ValueError(f'{table}: ID 중복')
    agents = {x['id'] for x in c['agents']}
    absence_ids = set()
    for entry in s.get('absences', []):
        if entry.get('id') in absence_ids or not entry.get('id'): raise ValueError('부재 일정 ID 오류')
        absence_ids.add(entry['id'])
        if entry.get('agent_id') not in agents: raise ValueError('등록되지 않은 부재 담당자')
        start = date.fromisoformat(entry['start'])
        end = date.fromisoformat(entry['end'])
        if end < start: raise ValueError('부재 종료일은 시작일 이후여야 합니다.')
    dates = [x['date'] for x in c['calendar']]
    if len(dates) != len(set(dates)): raise ValueError('제외 날짜가 중복됩니다.')
    for d in dates: date.fromisoformat(d)
    for a in c['agents']:
        if a['max_load'] < 1: raise ValueError('권장 업무량은 1 이상이어야 합니다.')
    works = set()
    for w in s.get('work_mappings', []):
        if not isinstance(w.get('part'), str) or not w['part'].strip() or not isinstance(w.get('work'), str) or not w['work'].strip():
            raise ValueError('세부업무 기준에 파트와 세부업무가 필요합니다.')
        if (w['part'], w['work']) in works: raise ValueError(f"세부업무 기준 중복: {w['part']}/{w['work']}")
        works.add((w['part'], w['work']))
        if not isinstance(w.get('active', True), bool): raise ValueError('세부업무 사용 여부 오류')
    from .pis import validate_settings
    from .catalog import validate as validate_catalog
    validate_settings(s); validate_catalog(c)
    if not isinstance(s.get('live_writes', False), bool): raise ValueError('실제 접수 설정 오류')
    # Google Sheets stores settings as one JSON cell (50,000 characters maximum).
    if len(json.dumps(s, ensure_ascii=False)) > 45000: raise ValueError('운영 설정이 너무 큽니다. 지난 젠콜 일정을 정리하세요.')
    return c

def ngrams(s):
    s = re.sub(r'\s+', '', s.casefold())
    return {s[i:i+2] for i in range(len(s)-1)}

def similarity(a,b):
    aa,bb=ngrams(a),ngrams(b)
    return len(aa&bb)/len(aa|bb) if aa and bb else 0.0

def is_pre_review_screen(text):
    """Conservative screen-specific recognition; generic 사전심사 mentions are not enough."""
    return bool(re.search(r'사전\s*심사\s*(?:화면|창|메뉴)',str(text or ''),re.IGNORECASE))

PRE_REVIEW_PARTS=('원무','보험')

def apply_pre_review_rule(assignments):
    """Ensure one 원무 and one 보험 row; people are filled from the catalogue and trimmed to one each by the caller."""
    output=copy_assignments(assignments)
    for part in PRE_REVIEW_PARTS:
        if not any(item['part']==part for item in output):
            output.append({'part':part,'part_id':None,'role':'UNDETERMINED',
                'responsibility':'사전심사 화면과 관련된 '+part+' 업무 확인','works':[],'supporting_ticket_ids':[]})
    return output

def copy_assignments(assignments):
    import copy
    return copy.deepcopy(assignments)

def lexical_search(text, history, hospital='', limit=5, completed_only=True):
    rows=[]
    for r in history:
        if completed_only and not r.get('completed'): continue
        if not r.get('summary'): continue
        score=similarity(text,r['summary'])
        if score < .03: continue
        score += .03 if hospital and r.get('hospital')==hospital else 0
        rows.append({'ticket_id':r['ticket_id'], 'score':round(score,4),
            'summary':r['summary'][:2000], 'parts':r.get('parts',[]), 'participants':r.get('participants',[])})
    return sorted(rows,key=lambda x:x['score'],reverse=True)[:limit]

def urgent_candidate(text):
    # Candidate only: false positives must go to human review.
    patterns = ['개인정보 유출','개인정보 노출','랜섬웨어','전체 시스템 접속 불가',
        '전체 사용자 로그인 불가','진료 전면 중단','수납 전면 중단','다수 사용자 로그인 불가']
    return next((p for p in patterns if p in text), '')
