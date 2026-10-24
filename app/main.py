"""AirScout command line. With no flags, starts the web setup page and scheduler."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import uvicorn
from . import scanner, store, whatsapp

async def dry_run():
    config=store.get_config()
    offers=await scanner.search_offers(config)
    if not offers:
        print('No offers returned for the saved trip.')
        return
    for offer in offers:
        print(json.dumps(offer,ensure_ascii=False))

async def once():
    result=await scanner.scan_once()
    print(f"Scan {result['scan_id']}: {len(result['offers'])} offers; best={result.get('best_price')}; WhatsApp sent={result.get('sent')}")

async def test_whatsapp():
    config=store.get_config()
    destinations,invalid=whatsapp.alert_destinations(config.get('group_id'))
    if not destinations:raise RuntimeError('No valid WhatsApp destinations configured. Check .env or choose a group in AirScout.')
    status=await whatsapp.status()
    if not status.get('connected'):raise RuntimeError(f"WhatsApp is not WORKING ({status.get('state')}).")
    failures=0
    for destination in destinations:
        try:await whatsapp.send_text(destination,'✈️ AirScout test: WhatsApp price alerts are connected.')
        except Exception:failures+=1
    if failures==len(destinations):raise RuntimeError('Test message could not be delivered. Check WhatsApp and configured destinations.')
    print(f"WAHA accepted test messages for {len(destinations)-failures} destination(s); failed: {failures}.")

def main():
    parser=argparse.ArgumentParser(description='AirScout flight price watcher')
    parser.add_argument('--once',action='store_true',help='Run one real scan and record it')
    parser.add_argument('--dry-run',action='store_true',help='Search and preview offers without sending or writing to SQLite')
    parser.add_argument('--test-whatsapp',action='store_true',help='Send one test message to the saved group')
    args=parser.parse_args()
    try:
        if args.dry_run:
            if not Path(store.DB_PATH).exists():raise RuntimeError('No saved AirScout search yet. Set up a trip in the web page first.')
            asyncio.run(dry_run())
        elif args.test_whatsapp:
            store.init();asyncio.run(test_whatsapp())
        elif args.once:
            store.init();asyncio.run(once())
        else:
            store.init();uvicorn.run('app.web:app',host='0.0.0.0',port=int(os.getenv('PORT','8080')))
    except Exception as e:raise SystemExit(str(e))

if __name__=='__main__':main()
