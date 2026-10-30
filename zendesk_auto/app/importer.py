from collections import defaultdict,Counter
from io import BytesIO
import re
from .domain import redact,NAMES

def split(v):return [s.strip() for s in re.split(r'[,\n]',str(v or '')) if s.strip()]

def import_xlsx(data):
    import openpyxl
    wb=openpyxl.load_workbook(BytesIO(data),read_only=True,data_only=True)
    grouped=defaultdict(list);tab_counts={};bad_ids=0
    try:
        for ws in wb:
            header=None;n=0
            for row in ws.iter_rows(values_only=True):
                if header is None:
                    normalized=[str(v or '').replace('\n','').replace(' ','').replace('*','') for v in row]
                    if '티켓번호' in normalized:header={h:i for i,h in enumerate(normalized) if h}
                    continue
                def val(k):
                    i=header.get(k);return row[i] if i is not None and i<len(row) else None
                raw=val('티켓번호')
                if raw is None:continue
                try:
                    num=float(raw)
                    if not num.is_integer() or num<=0:raise ValueError()
                    tid=int(num)
                except (ValueError,TypeError):bad_ids+=1;continue
                n+=1
                participants=[p for p in split(val('개발담당자/PIS담당자')) if p in NAMES]
                grouped[tid].append({'source':ws.title,'hospital':str(val('요청병원') or ''),
                    'summary':redact(val('요청내용요약')), 'parts':split(val('관련파트')),
                    'participants':participants,'type':str(val('구분') or ''),
                    'status':str(val('Zendesk실제상태') or ''), 'created_at':str(val('접수일시') or '')})
            tab_counts[ws.title]=n
    finally:wb.close()
    records=[]
    for tid,versions in grouped.items():
        states={v['status'] for v in versions};summaries=list(dict.fromkeys(v['summary'] for v in versions if v['summary']))
        records.append({'ticket_id':tid,'hospital':versions[0]['hospital'],
            'summary':'\n'.join(summaries)[:12000], 'parts':sorted({p for v in versions for p in v['parts']}),
            'participants':sorted({p for v in versions for p in v['participants']}),
            'sources':sorted({v['source'] for v in versions}), 'completed':states=={'처리완료'},
            'status_conflict':len(states)>1,'status':sorted(states), 'summary_conflict':len(summaries)>1,
            'created_at':versions[0]['created_at'], 'type':versions[0]['type']})
    return records,{'tabs':tab_counts,'rows':sum(tab_counts.values()),'unique':len(records),
        'extra_rows':sum(len(v)-1 for v in grouped.values()),'bad_ids':bad_ids,
        'missing_summary':sum(not r['summary'] for r in records),'completed':sum(r['completed'] for r in records)}
