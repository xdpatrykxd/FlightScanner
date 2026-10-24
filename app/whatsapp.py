import os, re, httpx

BASE=(os.getenv('WAHA_URL') or os.getenv('WHATSAPP_GATEWAY_URL') or 'http://localhost:3010').rstrip('/')
KEY=os.getenv('WAHA_API_KEY','')
SESSION=os.getenv('WAHA_SESSION') or os.getenv('WHATSAPP_SESSION','default')
TIMEOUT=float(os.getenv('WAHA_TIMEOUT_SECONDS') or os.getenv('WHATSAPP_GATEWAY_TIMEOUT_SECONDS','60'))

def configured_destinations(extra=None):
    """Return deduplicated WAHA chat IDs and names of invalid env settings."""
    values=[]
    for key,value in os.environ.items():
        if key.startswith('WHATSAPP_RECIPIENT_') and key[len('WHATSAPP_RECIPIENT_'):].isdigit() and value.strip():
            values.extend((key,item.strip()) for item in value.split(','))
    for key in ('WHATSAPP_RECIPIENTS','WHATSAPP_GROUP_IDS'):
        for item in os.getenv(key,'').split(','):
            if item.strip():values.append((key,item.strip()))
    for key,item in extra or []:values.append((key,item))
    destinations=[];invalid=[]
    for source,raw in values:
        value=raw.strip()
        if value.startswith('whatsapp:'):value=value[len('whatsapp:'):]
        if re.fullmatch(r'\+\d{7,15}',value):chat_id=value[1:]+'@c.us'
        elif re.fullmatch(r'\d+(?:-\d+)?@g\.us',value):chat_id=value
        else:
            invalid.append(source);continue
        if chat_id not in destinations:destinations.append(chat_id)
    return destinations,sorted(set(invalid))

def alert_destinations(group_id=None):
    extra=[('saved WhatsApp group',group_id)] if group_id else []
    return configured_destinations(extra)

def headers():
    return {'X-Api-Key':KEY}

async def request(method,path,**kwargs):
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        return await client.request(method,BASE+path,headers=headers(),**kwargs)

async def status():
    try:
        r=await request('GET',f'/api/sessions/{SESSION}')
        if r.status_code==404:return {'connected':False,'state':'STOPPED'}
        r.raise_for_status();data=r.json();state=data.get('status','UNKNOWN')
        return {'connected':state.upper()=='WORKING','state':state}
    except Exception as e:return {'connected':False,'state':'UNAVAILABLE','error':str(e)}

async def start():
    # Create the session when it does not exist, then ask WAHA to start it.
    r=await request('POST','/api/sessions',json={'name':SESSION})
    if r.status_code not in (200,201,409) and r.status_code != 422:
        r.raise_for_status()
    r=await request('POST',f'/api/sessions/{SESSION}/start')
    if r.status_code not in (200,201,202,409):r.raise_for_status()
    return await status()

async def qr():
    r=await request('GET',f'/api/{SESSION}/auth/qr')
    if r.status_code==404:
        await start();r=await request('GET',f'/api/{SESSION}/auth/qr')
    if not r.is_success: return None, r.status_code, r.text[:500]
    return r.content,r.status_code,r.headers.get('content-type','image/png')

async def groups():
    # WAHA's group list endpoint returns chats that are groups.
    r=await request('GET',f'/api/{SESSION}/groups')
    if r.status_code==404:
        r=await request('GET',f'/api/{SESSION}/chats',params={'limit':500})
    r.raise_for_status();data=r.json()
    if isinstance(data,dict):data=data.get('data',data.get('chats',data.get('groups',[])))
    result=[]
    for group in data if isinstance(data,list) else []:
        chat_id=group.get('id') or group.get('jid') or group.get('JID') or group.get('chatId')
        if not isinstance(chat_id,str) or not chat_id.endswith('@g.us'):continue
        name=group.get('name') or group.get('subject') or group.get('Name') or group.get('Subject') or chat_id
        result.append({'id':chat_id,'name':name})
    return result

async def send_text(chat_id,text):
    r=await request('POST','/api/sendText',json={'session':SESSION,'chatId':chat_id,'text':text,'linkPreview':True})
    r.raise_for_status();return r.json()
