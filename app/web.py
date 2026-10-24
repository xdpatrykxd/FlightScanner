import asyncio, os
import csv
from datetime import date
from functools import lru_cache
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from . import store, whatsapp, scanner

@asynccontextmanager
async def lifespan(app):
    store.init()
    task=asyncio.create_task(scanner.scheduler())
    yield
    task.cancel()
    try:await task
    except asyncio.CancelledError:pass

app=FastAPI(title='AirScout',lifespan=lifespan)
app.mount('/static',StaticFiles(directory='static'),name='static')
_scan_task=None

async def run_scan_job():
    try:
        result=await scanner.scan_all_once()
        scanner._progress.update(running=False,message='Price check complete.',result=result,error=None)
    except Exception as e:
        scanner._progress.update(running=False,message='Price check stopped.',error=str(e))

class DestinationAlert(BaseModel):
    origin_airports:list[str]=Field(min_length=1,max_length=5)
    origin_airport_names:list[str]=Field(default_factory=list,max_length=5)
    destination_airports:list[str]=Field(min_length=1,max_length=10)
    destination_airport_names:list[str]=Field(default_factory=list,max_length=10)
    departure_date:str
    return_date:str
    departure_start_date:str=''
    departure_end_date:str=''
    return_start_date:str=''
    return_end_date:str=''
    budget:float=Field(gt=0)
    min_price_drop_percent:int=Field(default=10,ge=1,le=90)
    passengers:int=Field(default=2,ge=1,le=9)
    cabin:str='economy'
    max_connections:int=Field(default=2,ge=0,le=4)
    group_id:str=''
    group_name:str=''
    enabled:bool=True

class Trip(BaseModel):
    origin:str=Field(min_length=3,max_length=3)
    destination:str=Field(min_length=3,max_length=3)
    origin_name:str=''
    destination_name:str=''
    origin_airports:list[str]=Field(default_factory=list,max_length=5)
    destination_airports:list[str]=Field(default_factory=list,max_length=5)
    origin_airport_names:list[str]=Field(default_factory=list,max_length=5)
    destination_airport_names:list[str]=Field(default_factory=list,max_length=5)
    departure_date:str
    return_date:str
    departure_start_date:str=''
    departure_end_date:str=''
    return_start_date:str=''
    return_end_date:str=''
    target_price:float=Field(gt=0)
    min_price_drop_percent:int=Field(default=10,ge=1,le=90)
    group_id:str=''
    group_name:str=''
    passengers:int=Field(default=2,ge=1,le=9)
    cabin:str='economy'
    max_connections:int=Field(default=2,ge=0,le=4)
    enabled:bool=True

def _normalize_window_dates(data):
    data['departure_start_date']=data.get('departure_start_date') or data['departure_date']
    data['departure_end_date']=data.get('departure_end_date') or data['departure_date']
    data['return_start_date']=data.get('return_start_date') or data['return_date']
    data['return_end_date']=data.get('return_end_date') or data['return_date']
    try:
        departure_start=date.fromisoformat(data['departure_start_date'])
        departure_end=date.fromisoformat(data['departure_end_date'])
        return_start=date.fromisoformat(data['return_start_date'])
        return_end=date.fromisoformat(data['return_end_date'])
    except (TypeError,ValueError):
        raise HTTPException(400,'Enter valid dates for all four date range fields.')
    if departure_end<departure_start or return_end<return_start:
        raise HTTPException(400,'Each date range must end on or after its start date.')
    if return_end<=departure_start:
        raise HTTPException(400,'The return date range must include a date after the earliest departure.')
    data['departure_date']=data['departure_start_date']
    data['return_date']=data['return_end_date']
    return data

@app.get('/')
async def index():return FileResponse('static/index.html')

@app.get('/admin')
async def admin():return FileResponse('static/admin.html')

@app.get('/README.md')
async def readme():return FileResponse('README.md',media_type='text/markdown')

@app.get('/api/state')
async def state():
    cfg=store.get_config();w=await whatsapp.status();scan=store.latest_scan()
    destinations,invalid=whatsapp.alert_destinations(cfg.get('group_id'))
    return {'config':cfg,'whatsapp':w,'scan':scan,'scan_status':scanner.scan_status(),'history':store.history(),'delivery':store.last_delivery_status(),'flight_provider_configured':True,'scan_interval_minutes':scanner.scan_interval_minutes(),'notification_destinations_count':len(destinations),'invalid_destination_vars':invalid}

@app.get('/api/places')
async def places(q:str):
    query=q.strip().casefold()
    airports=_airport_catalogue()
    # A code query should return that exact airport before other matches.
    matches=[p for p in airports if query in p['city'].casefold() or query in p['name'].casefold() or query in p['country'].casefold() or query==p['code'].casefold()]
    matches.sort(key=lambda p:(p['code'].casefold()!=query,p['city'].casefold(),p['name'].casefold()))
    return {'places':[{'code':p['code'],'city':p['city'],'name':p['name'],'country':p['country'],'type':'airport'} for p in matches[:40]]}


@lru_cache(maxsize=1)
def _airport_catalogue():
    path=os.path.join(os.path.dirname(os.path.dirname(__file__)),'config','airports.csv')
    airports=[]
    with open(path,encoding='utf-8',newline='') as source:
        for row in csv.DictReader(source):
            code=row.get('iata','').strip().upper()
            city=row.get('city','').strip()
            name=row.get('name','').strip()
            if len(code)==3 and city and name:
                airports.append({'code':code,'city':city,'name':name,'country':row.get('country','').strip().upper()})
    return airports

@app.put('/api/trip')
async def save_trip(t:Trip):
    data=_normalize_window_dates(t.model_dump())
    data['origin_airports']=list(dict.fromkeys(code.upper() for code in (data['origin_airports'] or [data['origin']])))
    data['destination_airports']=list(dict.fromkeys(code.upper() for code in (data['destination_airports'] or [data['destination']])))
    data['origin']=data['origin_airports'][0];data['destination']=data['destination_airports'][0]
    if {code.upper() for code in data['origin_airports']} & {code.upper() for code in data['destination_airports']}:raise HTTPException(400,'An airport cannot be selected as both an origin and destination.')
    if not whatsapp.alert_destinations(data['group_id'])[0]:raise HTTPException(400,'Add a valid WhatsApp destination in .env or choose a WhatsApp group for your alerts.')
    existing=store.get_config()
    if existing.get('destination_alert'):
        data['destination_alert']=existing['destination_alert']
    if not data.get('origin') and existing.get('origin'):
        data={**existing,**data}
    store.save_config(data)
    global _scan_task
    if not _scan_task or _scan_task.done():_scan_task=asyncio.create_task(run_scan_job())
    return {'saved':True,'config':data}

@app.put('/api/destination-alert')
async def save_destination_alert(alert:DestinationAlert):
    data=_normalize_window_dates(alert.model_dump())
    data['origin_airports']=list(dict.fromkeys(x.upper() for x in data['origin_airports']))
    data['destination_airports']=list(dict.fromkeys(x.upper() for x in data['destination_airports']))
    if {code.upper() for code in data['origin_airports']} & {code.upper() for code in data['destination_airports']}:raise HTTPException(400,'A departure airport cannot also be one of the destinations.')
    if not whatsapp.alert_destinations(data['group_id'])[0]:raise HTTPException(400,'Add a valid WhatsApp destination in .env or choose a WhatsApp group for your alerts.')
    config=store.get_config();config['destination_alert']=data;store.save_config(config)
    global _scan_task
    if not _scan_task or _scan_task.done():_scan_task=asyncio.create_task(run_scan_job())
    return {'saved':True,'destination_alert':data}

@app.post('/api/scan')
async def scan():
    global _scan_task
    if _scan_task and not _scan_task.done():return {'started':False,'running':True}
    _scan_task=asyncio.create_task(run_scan_job())
    return {'started':True,'running':True}

@app.get('/api/scan/status')
async def scan_status():return scanner.scan_status()

@app.get('/api/whatsapp/status')
async def wa_status():return await whatsapp.status()

@app.post('/api/whatsapp/connect')
async def wa_connect():
    try:return await whatsapp.start()
    except Exception as e:raise HTTPException(502,f'Could not start WhatsApp gateway: {e}')

@app.get('/api/whatsapp/qr')
async def wa_qr():
    content,status,meta=await whatsapp.qr()
    if content is None:raise HTTPException(502,f'WhatsApp QR is not ready: {meta}')
    return Response(content,media_type=meta)

@app.get('/api/whatsapp/groups')
async def wa_groups():
    try:return {'groups':await whatsapp.groups()}
    except Exception as e:raise HTTPException(502,f'Could not load groups. Make sure WhatsApp is linked: {e}')

class TestMessage(BaseModel):group_id:str=''
@app.post('/api/whatsapp/test')
async def wa_test(m:TestMessage):
    destinations,invalid=whatsapp.alert_destinations(m.group_id)
    if not destinations:raise HTTPException(400,'No valid WhatsApp destinations are configured. Replace placeholder values or choose a group.')
    failures=0
    for destination in destinations:
        try:await whatsapp.send_text(destination,'✈️ AirScout is connected! We’ll send flight price alerts here.')
        except Exception:failures+=1
    if failures==len(destinations):raise HTTPException(502,'Test message could not be delivered. Check that WhatsApp is linked and the destination is valid.')
    return {'sent':True,'sent_count':len(destinations)-failures,'failed_count':failures,'invalid_destination_vars':invalid}
