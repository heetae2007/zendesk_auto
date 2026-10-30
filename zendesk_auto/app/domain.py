"""Pure routing policy. No network or Zendesk writes in this module."""
import re
from datetime import datetime, date, time
from zoneinfo import ZoneInfo
from collections import Counter

KST = ZoneInfo('Asia/Seoul')
NAMES = ['김종오', '이선주', '김희재', '김성지', '김정진', '오형진', '박지원', '윤새라', '김구현', '이정기', '채희태', '신승용']

def defaults():
    return {
        'settings': {'enabled': False, 'start': '09:00', 'end': '18:00',
            'exclude_weekends': True, 'exclude_holidays': True, 'notify': False, 'ai_enabled': False,
            'min_confidence': .90, 'holiday_country': 'KR', 'mapping': {},
            'message': '안녕하세요. 문의하신 #[티켓번호]이 접수 되었습니다.\n\n담당 파트에 확인 후 회신 드리겠습니다.\n\n감사합니다.'},
        'agents': [{'id': f'agent-{i+1}', 'name': n, 'active': True, 'parts': [],
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
    seen = set()
    for r in c['roster']:
        date.fromisoformat(r['date'])
        key = (r['date'], r['part'])
        if key in seen: raise ValueError('같은 날짜·파트의 당번이 중복됩니다.')
        seen.add(key)
        if not set(r['primary'] + r['backup']) <= agents: raise ValueError('등록되지 않은 담당자')
    dates = [x['date'] for x in c['calendar']]
    if len(dates) != len(set(dates)): raise ValueError('제외 날짜가 중복됩니다.')
    for d in dates: date.fromisoformat(d)
    for a in c['agents']:
        if a['max_load'] < 1: raise ValueError('권장 업무량은 1 이상이어야 합니다.')
    for r in c['rules']:
        if not r.get('part'): raise ValueError('규칙에 담당 파트가 필요합니다.')
        if not r.get('hospital') and not r.get('keywords'): raise ValueError('규칙에 병원 또는 키워드 조건이 필요합니다.')
    return c

def rule_part(config, text, hospital):
    matches = []
    for r in config['rules']:
        if not r.get('active',True): continue
        if r.get('hospital') and r['hospital'] != hospital: continue
        keywords = r.get('keywords', [])
        if keywords and not any(k.casefold() in text.casefold() for k in keywords): continue
        matches.append(r)
    if not matches: return None
    matches.sort(key=lambda x: x['priority'], reverse=True)
    best = [x for x in matches if x['priority'] == matches[0]['priority']]
    if len({x['part'] for x in best}) > 1: return {'conflict': True}
    return {'part': best[0]['part'], 'rule_id': best[0]['id']}

def choose_agent(config, part, day, loads):
    roster = next((r for r in config['roster'] if r['date']==day and r['part']==part),None)
    if not roster: return None, '해당 날짜·파트의 담당자가 지정되지 않았습니다.'
    agents = {a['id']: a for a in config['agents']}
    for tier in ('primary','backup'):
        candidates = [agents[x] for x in roster[tier] if x in agents and agents[x]['active'] and part in agents[x]['parts']]
        candidates = [a for a in candidates if loads.get(a['id'],0) < a['max_load']]
        if candidates:
            return min(candidates,key=lambda a:(loads.get(a['id'],0)/a['max_load'], a['id'])), tier
    return None, '배정 가능한 주담당·대체 담당자가 없거나 권장 업무량을 초과했습니다.'

def ngrams(s):
    s = re.sub(r'\s+', '', s.casefold())
    return {s[i:i+2] for i in range(len(s)-1)}

def similarity(a,b):
    aa,bb=ngrams(a),ngrams(b)
    return len(aa&bb)/len(aa|bb) if aa and bb else 0.0

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
