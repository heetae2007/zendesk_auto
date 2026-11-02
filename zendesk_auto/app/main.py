import os,json,threading,secrets,fcntl,zipfile
from pathlib import Path
from io import BytesIO
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI,Depends,HTTPException,Request,UploadFile,File
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic,HTTPBasicCredentials
from .domain import validate_config,operating,KST,defaults,redact
from .store import Store,Sheets,revision
from .clients import Zendesk,AI,Telegram
from .engine import Engine
from .importer import import_xlsx
from .analytics import Analytics
from .telegram_format import format_notification

def load_env():
    p=Path('.env')
    if p.exists():
        for line in p.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k,v=line.split('=',1);os.environ.setdefault(k.strip(),v.strip().strip('"').strip("'"))
load_env()

def make_store():
    mode=os.getenv('STORE_MODE','local');sheets=None
    if mode=='sheets':sheets=Sheets(os.environ['GOOGLE_SPREADSHEET_ID'],os.environ['GOOGLE_APPLICATION_CREDENTIALS'])
    elif mode!='local':raise ValueError('STORE_MODE must be local or sheets')
    return Store(os.getenv('DATA_DIR','./data'),sheets)

store=make_store();analytics=Analytics(store);engine=Engine(store,Zendesk(),AI(store.root,analytics),Telegram(),analytics);stop=threading.Event()
security=HTTPBasic(auto_error=False)

def auth(creds:HTTPBasicCredentials|None=Depends(security)):
    expected=os.getenv('ADMIN_PASSWORD','')
    if len(expected)<16 or expected=='CHANGE_ME_TO_A_LONG_RANDOM_PASSWORD':raise HTTPException(503,'서버 ADMIN_PASSWORD를 16자 이상으로 설정하세요.')
    if not creds or not secrets.compare_digest(creds.username.encode(),b'admin') or not secrets.compare_digest(creds.password.encode(),expected.encode()):
        raise HTTPException(401,'로그인이 필요합니다.',headers={'WWW-Authenticate':'Basic realm="Zendesk Admin"'})
    return 'admin'

def loop():
    while not stop.wait(60):engine.tick()

@asynccontextmanager
async def lifespan(app):
    lock=open(store.root/'instance.lock','a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise RuntimeError('이미 실행 중입니다. workers=1로 실행하세요.')
    stop.clear();thread=threading.Thread(target=loop,daemon=True);thread.start()
    yield
    stop.set();thread.join(timeout=35);lock.close()

app=FastAPI(title='Zendesk 접수 운영실',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)

@app.middleware('http')
async def headers(request:Request,call_next):
    if request.method not in ('GET','HEAD','OPTIONS') and request.headers.get('x-requested-with')!='zendesk-admin':
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail':'요청 검증 실패'},status_code=403)
    response=await call_next(request)
    response.headers['Cache-Control']='no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    return response

@app.get('/',dependencies=[Depends(auth)])
def index():return FileResponse(Path(__file__).parent/'static/index.html')

@app.get('/api/state',dependencies=[Depends(auth)])
def state():
    c=store.config;allowed,why=operating(c)
    return {'config':c,'revision':revision(c),'mode':'추천 전용', 'storage':'Google Sheets' if store.sheets else '로컬 설정 테스트',
        'ready':store.ready,'operating':allowed,'operating_reason':why,'now':datetime.now(KST).isoformat(),
        'jobs':sorted(store.state['jobs'].values(),key=lambda x:x.get('updated_at',''),reverse=True)[:300],
        'history_count':len(store.history),'completed_count':sum(r.get('completed',False) for r in store.history),
        'last_tick':store.state['last_tick'],'last_error':store.state['last_error'],
        'notifications':store.state['notifications'],
        'connections':{'zendesk':bool(os.getenv('ZENDESK_API_TOKEN')),'claude':bool(os.getenv('ANTHROPIC_API_KEY') and os.getenv('ANTHROPIC_MODEL','claude-sonnet-4-6')),
            'telegram':bool(os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_CHAT_ID'))}}

@app.post('/api/config',dependencies=[Depends(auth)])
def config(body:dict):
    try:
        c=validate_config(body['config']);store.save_config(c,body['revision']);return {'revision':revision(c)}
    except (ValueError,KeyError,TypeError) as e:raise HTTPException(409,str(e))
    except Exception:raise HTTPException(503,'설정 저장 실패. 연결 상태를 확인하세요.')

@app.get('/api/calendar',dependencies=[Depends(auth)])
def calendar(year:int):
    if year<2020 or year>2100:raise HTTPException(400,'연도 범위 오류')
    import holidays
    return {d.isoformat():name for d,name in holidays.KR(years=year,language='ko').items()}

@app.post('/api/sheets/initialize',dependencies=[Depends(auth)])
def initialize():
    if not store.sheets:raise HTTPException(400,'서버에서 STORE_MODE=sheets 설정이 필요합니다.')
    try:
        with store.lock:store.sheets.initialize();store.refresh()
        return {'ok':True}
    except Exception:raise HTTPException(503,'시트 초기화 실패. 공유 권한과 API 설정을 확인하세요.')

@app.post('/api/config/refresh',dependencies=[Depends(auth)])
def refresh():
    try:
        with store.lock:store.refresh()
        return {'ok':True}
    except Exception:raise HTTPException(503,'시트 설정 읽기 실패')

@app.get('/api/zendesk/metadata',dependencies=[Depends(auth)])
def metadata():
    if not os.getenv('ZENDESK_API_TOKEN'):raise HTTPException(400,'Zendesk 인증정보를 서버에 설정하세요.')
    try:return engine.z.metadata()
    except Exception:raise HTTPException(503,'Zendesk 설정 조회 실패. API 권한을 확인하세요.')

@app.post('/api/run',dependencies=[Depends(auth)])
def run():return engine.tick()

@app.post('/api/jobs/{tid}/retry',dependencies=[Depends(auth)])
def retry(tid:int):
    if str(tid) not in store.state['jobs']:raise HTTPException(404,'티켓 없음')
    if engine.run_lock.locked():raise HTTPException(409,'분석 완료 후 다시 시도하세요.')
    # A new baseline is allowed only after explicit administrator request.
    try:t=engine.z.ticket(tid)
    except Exception:raise HTTPException(503,'티켓 재조회 실패')
    store.set_job(tid,status='queued',attempts=0,source_updated_at=t['updated_at']);return {'ok':True}

@app.post('/api/import',dependencies=[Depends(auth)])
async def upload(file:UploadFile=File(...)):
    data=await file.read(20*1024*1024+1)
    if len(data)>20*1024*1024:raise HTTPException(413,'20MB 이하 파일을 사용하세요.')
    try:
        with zipfile.ZipFile(BytesIO(data)) as z:
            if sum(x.file_size for x in z.infolist())>100*1024*1024:raise ValueError('압축 해제 용량 초과')
        rows,report=import_xlsx(data)
        if not rows:raise ValueError('티켓번호 열과 데이터를 찾을 수 없습니다.')
        store.import_history(rows);return report
    except Exception as e:raise HTTPException(400,'가져오기 실패: '+type(e).__name__)

@app.post('/api/simulate',dependencies=[Depends(auth)])
def simulate(body:dict):
    import copy
    text=redact(body.get('text',''))
    if not text.strip():raise HTTPException(400,'문의 내용을 입력하세요.')
    c=copy.deepcopy(store.config)
    # Local simulation uses a synthetic hospital field. No Zendesk requests.
    c['settings']['mapping']['hospital_field_id']='simulation'
    try:now=datetime.fromisoformat(body.get('at') or datetime.now(KST).isoformat())
    except (ValueError,TypeError):raise HTTPException(400,'테스트 일시가 올바르지 않습니다.')
    if now.tzinfo is None:now=now.replace(tzinfo=KST)
    allowed,why=operating(c,now)
    if not allowed:return {'status':'deferred','reason':why}
    try:result=engine.analyze({'id':0,'subject':text,'description':'','custom_fields':[{'id':'simulation','value':body.get('hospital','')}]},c,{},[],now,True)
    except Exception as e:
        from .clients import IntegrationError
        detail=str(e) if isinstance(e,IntegrationError) else type(e).__name__
        raise HTTPException(503,'분석 실패: '+detail)
    result['telegram_status']='not_requested'
    if body.get('send_telegram') is True:
        # Synthetic test only: no real ticket ID or Zendesk URL and no ticket mutation.
        message=format_notification(result,simulation=True)
        if not os.getenv('TELEGRAM_BOT_TOKEN') or not os.getenv('TELEGRAM_CHAT_ID'):
            result['telegram_status']='not_configured'
        else:
            try:
                engine.telegram.send(message)
                result['telegram_status']='sent'
            except Exception:
                # A timeout may happen after delivery. Do not automatically retry.
                result['telegram_status']='unknown'
    return result

@app.get('/api/analytics',dependencies=[Depends(auth)])
def analytics_dashboard(query:str=''):
    with analytics.lock:
        cases=list(analytics.cases.values())
        if query:
            q=query.strip().casefold()
            cases=[c for c in cases if q in str(c['ticket_id']) or q in (c.get('hospital') or '').casefold()]
        cases.sort(key=lambda c:(not bool(c.get('decision')),bool(c.get('review')),-c['ticket_id']))
        return {'summary':analytics.summary(),'cases':copy_data(cases[:250]),'total_matches':len(cases),
            'usage':copy_data(analytics.usage[-200:][::-1]),'prompts':copy_data(analytics.prompts[-30:][::-1]),
            'active_prompt':'base-0.8-pre-review'+('+'+str(store.config['settings']['prompt_improvement']['id']) if store.config['settings'].get('prompt_improvement') else '')}

def copy_data(obj):
    import copy
    return copy.deepcopy(obj)

@app.post('/api/analytics/import',dependencies=[Depends(auth)])
async def analytics_import(file:UploadFile=File(...)):
    data=await file.read(20*1024*1024+1)
    if len(data)>20*1024*1024:raise HTTPException(413,'20MB 이하 파일을 사용하세요.')
    try:
        with zipfile.ZipFile(BytesIO(data)) as z:
            if sum(x.file_size for x in z.infolist())>100*1024*1024:raise ValueError('압축 해제 용량 초과')
        names={a['name'] for a in store.config['agents'] if a.get('name')}
        rows,report=import_xlsx(data,names)
        if not rows:raise ValueError('티켓번호 열과 데이터를 찾을 수 없습니다.')
        return {'file':report,'comparison':analytics.import_cases(rows)}
    except Exception as e:raise HTTPException(400,'평가 파일 가져오기 실패: '+type(e).__name__)

@app.post('/api/analytics/review/{tid}',dependencies=[Depends(auth)])
def analytics_review(tid:int,body:dict):
    try:return analytics.review_case(tid,body)
    except (ValueError,TypeError,KeyError) as e:raise HTTPException(400,str(e))

@app.post('/api/analytics/propose',dependencies=[Depends(auth)])
def analytics_propose():
    try:return analytics.propose()
    except ValueError as e:raise HTTPException(400,str(e))

@app.post('/api/analytics/apply',dependencies=[Depends(auth)])
def analytics_apply(body:dict):
    try:
        analytics.apply(str(body['id']),str(body['revision']))
        return {'ok':True}
    except (ValueError,KeyError) as e:raise HTTPException(409,str(e))
    except Exception:raise HTTPException(503,'설정 저장 실패')

@app.post('/api/analytics/reset',dependencies=[Depends(auth)])
def analytics_reset(body:dict):
    try:
        c=copy_data(store.config);c['settings'].pop('prompt_improvement',None)
        store.save_config(c,str(body['revision']));return {'ok':True}
    except (ValueError,KeyError) as e:raise HTTPException(409,str(e))
    except Exception:raise HTTPException(503,'설정 저장 실패')

app.mount('/static',StaticFiles(directory=Path(__file__).parent/'static'),name='static')
