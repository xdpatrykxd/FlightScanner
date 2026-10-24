"""Small command-line controls for the WAHA linked-device gateway."""
import argparse
import asyncio
import os
from pathlib import Path
from . import store, whatsapp


def data_dir():
    return Path(os.getenv('DATABASE_PATH','data/listings.sqlite3')).parent

async def pair():
    result=await whatsapp.start()
    if result.get('connected'):
        print(f"WhatsApp session '{whatsapp.SESSION}' is already WORKING.")
        return
    path=data_dir()/'whatsapp-qr.png'
    print('Waiting for WAHA to create a QR code…')
    for _ in range(30):
        image,status,detail=await whatsapp.qr()
        if image:
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(image)
            print(f'QR saved to {path}. Open it on another screen and scan it now:')
            print('WhatsApp → Settings → Linked devices → Link a device.')
            break
        await asyncio.sleep(2)
    else:
        raise RuntimeError(f'WAHA did not provide a QR code ({status}: {detail}). Check the gateway logs and retry.')
    for _ in range(90):
        state=await whatsapp.status()
        if state.get('connected'):
            print('WhatsApp session is WORKING.')
            return
        await asyncio.sleep(2)
    print('Pairing is not confirmed yet. Run `python -m app.gateway status` to check it.')

async def status():
    result=await whatsapp.status()
    print(f"Session: {whatsapp.SESSION}\nStatus: {result.get('state','UNKNOWN')}")
    if result.get('error'):print(f"Gateway error: {result['error']}")
    if not result.get('connected'):raise RuntimeError('WhatsApp session is not WORKING.')

async def groups():
    result=await whatsapp.groups()
    if not result:
        print('No WhatsApp groups found for this linked account.')
        return
    print('WhatsApp groups:')
    for group in result:print(f"{group['id']}\t{group['name']}")

async def test(chat_id=None):
    store.init()
    cfg=store.get_config()
    destinations,invalid=whatsapp.alert_destinations(chat_id or cfg.get('group_id'))
    if not destinations:raise RuntimeError('No valid WhatsApp destinations configured. Add one in .env or pass a WhatsApp destination.')
    connection=await whatsapp.status()
    if not connection.get('connected'):
        raise RuntimeError(f"WhatsApp session is not WORKING ({connection.get('state')}).")
    failures=0
    for destination in destinations:
        try:await whatsapp.send_text(destination,'✈️ AirScout test: WhatsApp price alerts are connected.')
        except Exception:failures+=1
    if failures==len(destinations):raise RuntimeError('Test messages could not be delivered. Check WhatsApp and configured destinations.')
    print(f"WAHA accepted test messages for {len(destinations)-failures} destination(s); failed: {failures}.")

async def run(command,chat_id=None):
    if command=='pair':await pair()
    elif command=='status':await status()
    elif command=='groups':await groups()
    elif command=='test':await test(chat_id)

def main():
    parser=argparse.ArgumentParser(description='Manage AirScout WhatsApp connection')
    parser.add_argument('command',choices=['pair','status','groups','test'])
    parser.add_argument('--chat-id',help='Optional WhatsApp group ID for a test message')
    args=parser.parse_args()
    try:asyncio.run(run(args.command,args.chat_id))
    except Exception as e:raise SystemExit(str(e))

if __name__=='__main__':main()
