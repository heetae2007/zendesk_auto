"""Telegram HTML messages: escape all variable text and link only known ticket IDs."""
from html import escape
from .domain import redact,allowed_agent

def _safe(value, limit):
    return escape(redact(value or '')[:limit],quote=True)

def format_notification(result, ticket_id=None, simulation=False):
    hospital=_safe(result.get('hospital') or '병원 확인 필요',100)
    urgent_prefix='[긴급] ' if result.get('urgent') else ''
    entries=result.get('recommended_assignments')
    if entries:
        lines=[]
        for entry in entries:
            people=[]
            for agent in entry.get('agents',[]):
                raw=agent.get('agent_name','')
                if not allowed_agent({'name':raw}):continue
                name=_safe(raw,80)
                if name:people.append(name if name.endswith('책임님') else name+' 책임님')
            lines.append(f"{_safe(entry.get('part') or '담당파트 확인 필요',80)} {', '.join(people) or '담당자 미정'}")
        heading=urgent_prefix+f"[{hospital}] " + ' / '.join(lines)
    else:
        part=_safe(result.get('part') or '담당파트 확인 필요',80)
        raw=result.get('agent_name') or ''
        person=_safe(raw,80) if allowed_agent({'name':raw}) else ''
        if person and not person.endswith('책임님'):person+=' 책임님'
        heading=urgent_prefix+f'[{hospital}] {part} {person or "담당자 미정"}'
    # The raw ticket is never sent; only generated, pattern-masked summaries.
    summary=_safe(result.get('summary'),350) if result.get('confidence') is not None else ''
    if not summary:summary='문의 요약을 확인해 주세요.'
    if simulation:
        return f'[테스트]\n{heading}\n\n{summary}\n\n가상 티켓 (실제 Zendesk 티켓 아님)'
    tid=int(ticket_id)
    if tid<=0:raise ValueError('유효하지 않은 티켓번호')
    link=f'https://edgennext.zendesk.com/agent/tickets/{tid}'
    return f'{heading}\n\n{summary}\n\n<a href="{link}">티켓: #{tid}</a>'
