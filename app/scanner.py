import os, asyncio, httpx, hashlib, json
from datetime import datetime, timezone, date, timedelta
from . import store, whatsapp, google_flights
import portalocker

_scan_lock=asyncio.Lock()
_request_slots=asyncio.Semaphore(max(1,int(os.getenv('REQUEST_CONCURRENCY','4'))))
_progress={'running':False,'completed':0,'total':0,'message':'Ready for a price check.','detail':'','stage':'idle','route_completed':0,'route_total':0,'offers':[],'error':None}

def scan_interval_minutes():return max(1,int(os.getenv('CHECK_INTERVAL_MINUTES') or os.getenv('SCAN_INTERVAL_MINUTES','1440')))
def request_timeout_seconds():return max(1,float(os.getenv('REQUEST_TIMEOUT_SECONDS','25')))
def is_major_drop(price,average,sample_count,minimum_percent,currency='EUR'):
    threshold=max(1.0,min(90.0,float(minimum_percent or 10)))
    return bool(currency=='EUR' and average and sample_count>=3 and price<=average*(1-threshold/100))
def scan_status():return dict(_progress)

def validate_config(c,require_group=False):
    required=['origin','destination','departure_date','return_date','target_price']
    missing=[k for k in required if not c.get(k)]
    if missing:raise ValueError('Missing required trip settings: '+', '.join(missing))
    try:
        start=date.fromisoformat(c.get('departure_start_date') or c['departure_date'])
        start_end=date.fromisoformat(c.get('departure_end_date') or c['departure_date'])
        return_start=date.fromisoformat(c.get('return_start_date') or c['return_date'])
        end=date.fromisoformat(c.get('return_end_date') or c['return_date'])
    except (TypeError,ValueError):raise ValueError('Enter valid dates for both date ranges.')
    if start_end<start or end<return_start:raise ValueError('Each date range must end on or after its start date.')
    if end<=start:raise ValueError('The return date range must include at least one date after departure.')
    if require_group and not whatsapp.alert_destinations(c.get('group_id'))[0]:
        _,invalid=whatsapp.alert_destinations(c.get('group_id'))
        hint=' Correct '+', '.join(invalid)+'.' if invalid else ' Add valid WhatsApp destinations in .env or choose a group in AirScout.'
        raise ValueError('No valid WhatsApp destinations are configured.'+hint)
    origins=c.get('origin_airports') or [c['origin']];destinations=c.get('destination_airports') or [c['destination']]
    if any(origin.upper()==destination.upper() for origin in origins for destination in destinations):raise ValueError('Choose at least one different origin and destination airport.')

async def search_offers(c,mode='trip'):
    validate_config(c)
    origins=c.get('origin_airports') or [c['origin']]
    destinations=c.get('destination_airports') or [c['destination']]
    route_groups={}
    for origin in origins:
      for destination in destinations:
        if origin.upper()==destination.upper():continue
        origin_city=google_flights.city_for_airport(origin)
        destination_city=google_flights.city_for_airport(destination)
        key=(origin.upper(),destination.upper())
        route_groups[key]={'origin_city':origin_city,'destination_city':destination_city,'origins':[origin.upper()],'destinations':[destination.upper()]}
    total=len(route_groups)
    offers=[];errors=[];pages=[];completed=0;read_fares=False
    _progress.update(route_completed=0,route_total=total)
    for group in route_groups.values():
        origin=group['origins'][0];destination=group['destinations'][0]
        _progress.update(running=True,stage='Searching airport pair',route_completed=completed,route_total=total,detail=f'{group["origin_city"]} ({origin}) → {group["destination_city"]} ({destination}) · pair {completed+1} of {total}')
        route={**c,'origin':origin,'destination':destination,'origin_city':group['origin_city'],'destination_city':group['destination_city'],'origin_airports':group['origins'],'destination_airports':group['destinations']}
        try:
            async with _request_slots:found,body,url=await google_flights.search(route,request_timeout_seconds())
            pages.append(body)
            for offer in found:
                read_fares=True
                if int(c.get('max_connections',2)) < offer['itinerary'][0]['stops']:continue
                offer['source_url']=url
                offer['passengers']=max(1,int(c.get('passengers',1) or 1))
                if mode=='explore':offer['destination_city']=group['destination_city']
                offers.append(offer)
        except Exception as e:errors.append(e)
        completed+=1
        offers.sort(key=lambda offer:offer['price'])
        _progress.update(route_completed=completed,route_total=total,offers=offers[:20],detail=f'Checked {group["origin_city"]} ({origin}) → {group["destination_city"]} ({destination}) · {completed} of {total} airport pairs')
    if not offers and errors:raise RuntimeError(f'Google Flights search failed for every airport combination: {errors[0]}')
    if not offers:
        no_flights=('no flights','no results','didn’t find any flights','did not find any flights',"couldn't find flights","could not find flights")
        if read_fares or any(phrase in body.lower() for body in pages for phrase in no_flights):return []
        raise RuntimeError('Google Flights loaded, but AirScout could not read the fare cards. Try again later or check the AirScout logs.')
    offers.sort(key=lambda offer:offer['price']);return offers[:20]

async def search_date_range(c,mode='trip'):
    """Compare valid departure/return pairs strictly inside user-selected ranges."""
    departure=date.fromisoformat(c.get('departure_start_date') or c['departure_date'])
    departure_end=date.fromisoformat(c.get('departure_end_date') or c['departure_date'])
    returning=date.fromisoformat(c.get('return_start_date') or c['return_date'])
    return_end=date.fromisoformat(c.get('return_end_date') or c['return_date'])
    candidates=[]
    for out in (departure+timedelta(days=n) for n in range((departure_end-departure).days+1)):
        for back in (returning+timedelta(days=n) for n in range((return_end-returning).days+1)):
            if back<=out:continue
            candidates.append((out,back))
    candidates.sort(key=lambda pair:(pair[0],pair[1]))
    combined=[]
    alert_label='destination alert' if mode=='explore' else 'trip alert'
    for index,(out,back) in enumerate(candidates,1):
        candidate={**c,'departure_date':out.isoformat(),'return_date':back.isoformat()}
        _progress.update(running=True,stage=f'Checking {alert_label}',completed=index-1,total=len(candidates),route_completed=0,route_total=0,message=f'Date option {index} of {len(candidates)}',detail=f'{out.strftime("%d %b %Y")} → {back.strftime("%d %b %Y")} · preparing airport searches')
        offers=await search_offers(candidate,mode)
        for offer in offers:
            offer['searched_departure_date']=candidate['departure_date']
            offer['searched_return_date']=candidate['return_date']
            offer['departure_date_offset']=(out-departure).days
            offer['return_date_offset']=(back-returning).days
        combined.extend(offers)
        combined.sort(key=lambda offer:offer['price'])
        _progress.update(running=True,completed=index,total=len(candidates),message=f'Date option {index} of {len(candidates)} complete',detail=(f'Best so far €{combined[0]["price"]:,.0f}' if combined else 'No readable fares for this date option'))
    if not combined:return [],c
    combined.sort(key=lambda offer:offer['price'])
    anchor_date_offers=[offer for offer in combined if offer.get('departure_date_offset')==0 and offer.get('return_date_offset')==(return_end-returning).days]
    if anchor_date_offers:
        selected_date_price=min(offer['price'] for offer in anchor_date_offers)
        for offer in combined:
            offer['selected_dates_price']=selected_date_price
            offer['savings_vs_selected_dates']=max(0,selected_date_price-offer['price'])
    best=combined[0]
    actual={**c,'departure_date':best['searched_departure_date'],'return_date':best['searched_return_date']}
    return combined[:20],actual

def city_for(code,c):
    names=c.get('destination_airport_names') or []
    codes=c.get('destination_airports') or []
    try:return names[codes.index(code)]
    except (ValueError,IndexError):return code

async def _trip_scan(c):
    validate_config(c,require_group=True)
    offers,actual=await search_date_range(c)
    if not offers:
        sid=store.log_scan(offers=[])
        return {'scan_id':sid,'offers':[],'best_price':None,'sent':False,'alert_type':'trip','searched_departure_date':None,'searched_return_date':c['return_date']}
    best=offers[0];target=float(c.get('target_price',0));sent=False
    destinations,_=whatsapp.alert_destinations(c.get('group_id'))
    criteria={key:actual.get(key) for key in ('origin_airports','destination_airports','origin','destination','departure_date','return_date','departure_start_date','departure_end_date','return_start_date','return_end_date','passengers','cabin','max_connections','min_price_drop_percent')}
    criteria['type']='trip'
    trip_key=hashlib.sha256(json.dumps(criteria,sort_keys=True).encode()).hexdigest()
    samples=store.recent_route_prices(best['itinerary'][0]['from'],best['itinerary'][0]['to'],actual['departure_date'],actual['return_date'])
    average=sum(samples)/len(samples) if samples else None
    major_drop=is_major_drop(best['price'],average,len(samples),c.get('min_price_drop_percent',10),best['currency'])
    sid=store.log_scan(best['price'],best['currency'],offers)
    if best['currency']=='EUR' and ((target>0 and best['price']<=target) or major_drop):
        latest=store.get_config()
        watched=('origin_airports','destination_airports','origin','destination','departure_date','return_date','departure_start_date','departure_end_date','return_start_date','return_end_date','target_price','group_id','cabin','passengers','max_connections','min_price_drop_percent')
        if any(latest.get(k)!=c.get(k) for k in watched):return {'scan_id':sid,'offers':offers,'best_price':best['price'],'sent':False,'stale':True,'alert_type':'trip'}
        stop_summary=' / '.join('nonstop' if leg['stops']==0 else f"{leg['stops']} stop" if leg['stops']==1 else f"{leg['stops']} stops" for leg in best['itinerary'])
        drop_line=f"Major drop: {((average-best['price'])/average*100):.0f}% below recent average (EUR {average:.0f}).\n" if major_drop else ''
        msg=(f"✈️ AirScout price alert\n{best['itinerary'][0]['from']} → {best['itinerary'][0]['to']}\n"
             f"{actual['departure_date']} to {actual['return_date']} · {c.get('passengers',2)} traveler(s) · {c.get('cabin','economy').replace('_',' ')}\n"
             f"EUR {best['price']:.0f} total for {c.get('passengers',2)} traveler(s), round trip · {stop_summary}\n{drop_line}Your target: EUR {target:.0f}\nAirline: {best['airline']}\n"
             "Open AirScout to view this offer. Recheck the live fare before booking.")
        sent=await deliver(sid,destinations,trip_key,best['price'],msg)
    return {'scan_id':sid,'offers':offers,'best_price':best['price'],'sent':sent,'alert_type':'trip','searched_departure_date':actual['departure_date'],'searched_return_date':actual['return_date']}

async def _explore_scan(config):
    c={**config,'origin':(config.get('origin_airports') or [config.get('origin','')])[0],
       'destination':(config.get('destination_airports') or [''])[0],
       'departure_date':config.get('departure_date'),'return_date':config.get('return_date'),
       'target_price':config.get('budget',0),'max_connections':config.get('max_connections',2)}
    validate_config(c,require_group=True)
    offers,actual=await search_date_range(c,'explore')
    if not offers:
        sid=store.log_scan(offers=[]);return {'scan_id':sid,'offers':[],'best_price':None,'sent':False,'alert_type':'explore','searched_departure_date':None,'searched_return_date':c['return_date']}
    best=offers[0];budget=float(c['target_price']);sent=False
    destinations,_=whatsapp.alert_destinations(config.get('group_id'))
    watched=('origin_airports','destination_airports','departure_date','return_date','departure_start_date','departure_end_date','return_start_date','return_end_date','budget','group_id','passengers','cabin','max_connections','min_price_drop_percent')
    trip_key=hashlib.sha256(json.dumps({'type':'explore',**{k:actual.get(k) if k in ('departure_date','return_date') else config.get(k) for k in watched}},sort_keys=True).encode()).hexdigest()
    samples=store.recent_route_prices(best['itinerary'][0]['from'],best['itinerary'][0]['to'],actual['departure_date'],actual['return_date'])
    average=sum(samples)/len(samples) if samples else None
    major_drop=is_major_drop(best['price'],average,len(samples),config.get('min_price_drop_percent',10),best['currency'])
    sid=store.log_scan(best['price'],best['currency'],offers)
    if best['currency']=='EUR' and (best['price']<=budget or major_drop):
        latest=store.get_config().get('destination_alert',{})
        watched=('origin_airports','destination_airports','departure_date','return_date','departure_start_date','departure_end_date','return_start_date','return_end_date','budget','group_id','passengers','cabin','max_connections','min_price_drop_percent')
        if any(latest.get(k)!=config.get(k) for k in watched):return {'scan_id':sid,'offers':offers,'best_price':best['price'],'sent':False,'stale':True,'alert_type':'explore'}
        city=best.get('destination_city') or best['itinerary'][0]['to']
        drop_line=f"Major drop: {((average-best['price'])/average*100):.0f}% below recent average (EUR {average:.0f}).\n" if major_drop else ''
        msg=(f"✈️ AirScout cheapest destination\n{city} · {best['itinerary'][0]['to']}\n"
             f"{actual['departure_date']} to {actual['return_date']} · {c.get('passengers',2)} traveler(s)\n"
             f"Current cheapest: EUR {best['price']:.0f} total for {c.get('passengers',2)} traveler(s), round trip\n{drop_line}Budget: EUR {budget:.0f}\nAirline: {best['airline']}\n"
             "Open AirScout to compare the other places. Recheck the live fare before booking.")
        sent=await deliver(sid,destinations,trip_key,best['price'],msg)
    return {'scan_id':sid,'offers':offers,'best_price':best['price'],'sent':sent,'alert_type':'explore','searched_departure_date':actual['departure_date'],'searched_return_date':actual['return_date']}

async def deliver(sid,destinations,trip_key,price,msg):
    sent=False;delivery_errors=[]
    for destination in destinations:
        prev_price=store.last_alert_price(destination,trip_key)
        if prev_price is not None and price>=float(prev_price):continue
        try:
            result=await whatsapp.send_text(destination,msg);store.record_delivery(sid,'accepted','WAHA accepted message',destination,trip_key,price,result.get('id'));sent=True
        except Exception as e:
            clear_rejection=isinstance(e,(httpx.ConnectError,httpx.ConnectTimeout)) or (isinstance(e,httpx.HTTPStatusError) and e.response.status_code<500)
            store.record_delivery(sid,'rejected' if clear_rejection else 'unknown',str(e),destination,trip_key,price);delivery_errors.append(e)
    if not sent and delivery_errors and len(delivery_errors)==len(destinations):raise RuntimeError(f'WhatsApp delivery failed for all {len(destinations)} configured destinations. Check the WAHA session and destination IDs.')
    return sent

async def _run_scans():
    results=[];errors=[];config=store.get_config()
    if config.get('enabled') and config.get('origin'):
        try:results.append(await _trip_scan(config))
        except Exception as e:store.log_scan(error=str(e));errors.append(str(e))
    explore=config.get('destination_alert') or {}
    if explore.get('enabled') and explore.get('destination_airports'):
        try:results.append(await _explore_scan(explore))
        except Exception as e:store.log_scan(error=str(e));errors.append(str(e))
    offers=sorted([o for r in results for o in r.get('offers',[])],key=lambda o:o['price'])
    return {'results':results,'offers':offers[:20],'best_price':offers[0]['price'] if offers else None,'sent':any(r.get('sent') for r in results),'errors':errors}

async def scan_all_once():
    _progress.update(running=True,completed=0,total=0,route_completed=0,route_total=0,stage='Starting',detail='Loading saved alerts',message='Starting price checks…',offers=[],error=None,result=None)
    async with _scan_lock:
        path=store.DB_PATH+'.lock'
        try:
            with portalocker.Lock(path,mode='a',timeout=0):return await _run_scans()
        except portalocker.exceptions.AlreadyLocked:raise RuntimeError('Another AirScout scan is using the database. Wait for it to finish, then retry.')
        finally:_progress.update(running=False)

async def scan_once():
    _progress.update(running=True,completed=0,total=0,route_completed=0,route_total=0,stage='Starting',detail='Loading saved alert',message='Checking the saved flight alert…',offers=[],error=None,result=None)
    async with _scan_lock:
        path=store.DB_PATH+'.lock'
        try:
            with portalocker.Lock(path,mode='a',timeout=0):
                c=store.get_config();return await _trip_scan(c)
        except portalocker.exceptions.AlreadyLocked:raise RuntimeError('Another AirScout scan is using the database. Wait for it to finish, then retry.')
        finally:_progress.update(running=False)

async def scheduler():
    while True:
        try:
            c=store.get_config();latest=store.latest_scan();elapsed=scan_interval_minutes()*60
            if latest:
                last=datetime.fromisoformat(latest['searched_at']);elapsed-=max(0,(datetime.now(timezone.utc)-last).total_seconds())
            if elapsed<=0 and ((c.get('enabled') and c.get('origin')) or (c.get('destination_alert') or {}).get('enabled')):await scan_all_once()
        except Exception as e:print(f'AirScout scan error: {e}',flush=True)
        await asyncio.sleep(min(60,scan_interval_minutes()*60))
