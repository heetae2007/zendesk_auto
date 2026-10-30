"""One application writer. Sheets is canonical when configured; no SQL DB."""
import os, json, tempfile, threading, copy, hashlib
from pathlib import Path
from datetime import datetime, timezone
from .domain import defaults

def stamp(): return datetime.now(timezone.utc).isoformat()

def atomic_json(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,name=tempfile.mkstemp(dir=path.parent, prefix='.write-')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(value,f,ensure_ascii=False,allow_nan=False);f.flush();os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)

def revision(c): return hashlib.sha256(json.dumps(c,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:20]

class Store:
    def __init__(self,root,sheets=None):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.lock=threading.RLock();self.sheets=sheets
        self.state=self.read('state',{'jobs':{},'scan':{},'last_error':None,'notifications':{},'last_tick':None})
        self.history=self.read('history',[])
        self.config=defaults() if sheets else self.read('config',defaults())
        self.ready=not bool(sheets)

    def read(self,name,default):
        p=self.root/(name+'.json')
        return json.loads(p.read_text()) if p.exists() else copy.deepcopy(default)

    def save_state(self): atomic_json(self.root/'state.json',self.state)

    def refresh(self):
        if self.sheets:
            c=self.sheets.load_config()
            if c is None: raise ValueError('Google Sheets 초기화가 필요합니다.')
            self.config=c; self.ready=True
            atomic_json(self.root/'config.json',c)
        return self.config

    def save_config(self,c,expected):
        with self.lock:
            if self.sheets: self.refresh()
            if revision(self.config)!=expected: raise ValueError('다른 사용자가 설정을 변경했습니다. 새로고침 후 다시 저장하세요.')
            before=self.config
            if self.sheets: self.sheets.save_config(c)
            atomic_json(self.root/'config.json',c); self.config=copy.deepcopy(c)
            audit=self.read('config_audit',[])
            audit.append({'at':stamp(),'actor':'admin','before':before,'after':c})
            atomic_json(self.root/'config_audit.json',audit[-200:])

    def set_job(self,tid,**changes):
        with self.lock:
            key=str(tid); j=self.state['jobs'].setdefault(key,{'ticket_id':int(tid)})
            j.update(changes);j['updated_at']=stamp();self.save_state();return j

    def import_history(self,rows):
        with self.lock:
            if self.sheets: self.sheets.write_rows('History',rows)
            atomic_json(self.root/'history.json',rows);self.history=rows

    def mirror(self):
        if not self.sheets:return
        self.sheets.write_rows('Decisions',list(self.state['jobs'].values()))

class Sheets:
    TABS=['Settings','Agents','Rules','Roster','Calendar','Feedback','Hospitals','History','Decisions']
    MAP={'settings':'Settings','agents':'Agents','rules':'Rules','roster':'Roster','calendar':'Calendar','feedback':'Feedback','hospitals':'Hospitals'}
    def __init__(self,spreadsheet_id,credentials):
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
        creds=service_account.Credentials.from_service_account_file(credentials,scopes=['https://www.googleapis.com/auth/spreadsheets'])
        self.session=AuthorizedSession(creds)
        self.base=f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}'

    def request(self,method,path,**kw):
        r=self.session.request(method,self.base+path,timeout=30,**kw)
        if r.status_code>=400: raise RuntimeError(f'Google Sheets HTTP {r.status_code}; 인증·공유·할당량 확인 필요')
        return r.json()

    def initialize(self):
        meta=self.request('GET','',params={'fields':'sheets.properties.title'})
        names={s['properties']['title'] for s in meta.get('sheets',[])}
        requests=[{'addSheet':{'properties':{'title':n,'gridProperties':{'rowCount':5000,'columnCount':3}}}} for n in self.TABS if n not in names]
        if requests:self.request('POST',':batchUpdate',json={'requests':requests})
        if self.load_config() is None:self.save_config(defaults())

    def load_config(self):
        r=self.request('GET','/values:batchGet',params=[('ranges',f"'{v}'!A2:C") for v in self.MAP.values()])
        c={}
        for (key,_),vr in zip(self.MAP.items(),r.get('valueRanges',[])):
            values=[json.loads(row[1]) for row in vr.get('values',[]) if len(row)>1 and row[1]]
            if key=='settings':
                if not values:return None
                c[key]=values[0]
            else:c[key]=values
        if set(c)!=set(self.MAP):raise ValueError('시트 구조가 올바르지 않습니다.')
        return c

    def save_config(self,c):
        data=[]
        for key,tab in self.MAP.items():
            rows=[c[key]] if key=='settings' else c[key]
            data.extend(self.values_update(tab,rows))
        self.request('POST',':batchUpdate',json={'requests':data})

    def values_update(self,tab,rows):
        # updateCells with a full A:C range clears obsolete rows in one atomic request.
        meta=self.request('GET','',params={'fields':'sheets.properties'})
        prop=next(s['properties'] for s in meta['sheets'] if s['properties']['title']==tab)
        values=[['ID','JSON (웹 화면에서 수정)','갱신 시각']]
        values += [[str(r.get('id',r.get('ticket_id',i))),json.dumps(r,ensure_ascii=False),stamp()] for i,r in enumerate(rows)]
        req=[]
        if len(values)>prop['gridProperties']['rowCount']:
            req.append({'appendDimension':{'sheetId':prop['sheetId'],'dimension':'ROWS','length':len(values)-prop['gridProperties']['rowCount']}})
        req.append({'updateCells':{'range':{'sheetId':prop['sheetId'],'startRowIndex':0,'startColumnIndex':0,'endColumnIndex':3},
            'rows':[{'values':[{'userEnteredValue':{'stringValue':v}} for v in row]} for row in values], 'fields':'userEnteredValue'}})
        return req

    def write_rows(self,tab,rows):self.request('POST',':batchUpdate',json={'requests':self.values_update(tab,rows)})
