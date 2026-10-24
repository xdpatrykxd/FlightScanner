import json, os, sqlite3
from datetime import datetime, timezone

DB_PATH = os.getenv('DATABASE_PATH', 'data/listings.sqlite3')

def connect():
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    db=sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory=sqlite3.Row
    return db

def init():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS scans (id INTEGER PRIMARY KEY, searched_at TEXT NOT NULL, best_price REAL, currency TEXT, offers TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS deliveries (id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL, sent_at TEXT NOT NULL, status TEXT NOT NULL, detail TEXT, FOREIGN KEY(scan_id) REFERENCES scans(id));
        ''')
        columns={row['name'] for row in db.execute('PRAGMA table_info(deliveries)')}
        for name,definition in [('recipient',"TEXT NOT NULL DEFAULT ''"),('trip_key',"TEXT NOT NULL DEFAULT ''"),('price','REAL'),('message_id','TEXT')]:
            if name not in columns:db.execute(f'ALTER TABLE deliveries ADD COLUMN {name} {definition}')
        if not db.execute('SELECT 1 FROM settings WHERE id=1').fetchone():
            db.execute('INSERT INTO settings(id,config) VALUES(1,?)',(json.dumps({}),))

def get_config():
    with connect() as db:
        row=db.execute('SELECT config FROM settings WHERE id=1').fetchone()
        return json.loads(row['config']) if row else {}

def save_config(config):
    with connect() as db:
        db.execute('INSERT INTO settings(id,config) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config',(json.dumps(config),))

def log_scan(best_price=None,currency=None,offers=None,error=None):
    with connect() as db:
        cur=db.execute('INSERT INTO scans(searched_at,best_price,currency,offers,error) VALUES(?,?,?,?,?)',(datetime.now(timezone.utc).isoformat(),best_price,currency,json.dumps(offers or []),error))
        return cur.lastrowid

def latest_scan():
    with connect() as db:
        row=db.execute('SELECT * FROM scans ORDER BY id DESC LIMIT 1').fetchone()
        if not row:return None
        item=dict(row);item['offers']=json.loads(item['offers']);return item

def history(limit=30):
    with connect() as db:
        return [dict(r) for r in db.execute('SELECT searched_at,best_price,currency FROM scans WHERE error IS NULL ORDER BY id DESC LIMIT ?', (limit,))]

def recent_route_prices(origin, destination, departure_date, return_date, limit=10):
    """Recent fare samples for this route and travel window, newest first."""
    with connect() as db:
        rows=db.execute("SELECT offers FROM scans WHERE error IS NULL AND offers IS NOT NULL ORDER BY id DESC LIMIT 100").fetchall()
    prices=[]
    for row in rows:
        try: offers=json.loads(row['offers'])
        except (TypeError,ValueError): continue
        scan_prices=[]
        for offer in offers:
            legs=offer.get('itinerary') or []
            if (legs and str(legs[0].get('from','')).upper()==str(origin).upper()
                and str(legs[0].get('to','')).upper()==str(destination).upper()
                and offer.get('searched_departure_date')==departure_date
                and offer.get('searched_return_date')==return_date and offer.get('currency')=='EUR'):
                try: scan_prices.append(float(offer['price']))
                except (TypeError,ValueError): pass
        if scan_prices: prices.append(min(scan_prices))
        if len(prices)>=limit: break
    return prices[:limit]

def record_delivery(scan_id,status,detail='',recipient='',trip_key='',price=None,message_id=None):
    with connect() as db:
        db.execute('INSERT INTO deliveries(scan_id,sent_at,status,detail,recipient,trip_key,price,message_id) VALUES(?,?,?,?,?,?,?,?)',(scan_id,datetime.now(timezone.utc).isoformat(),status,detail,recipient,trip_key,price,message_id))

def last_alert_price(recipient,trip_key):
    with connect() as db:
        row=db.execute("SELECT price FROM deliveries WHERE recipient=? AND trip_key=? AND status IN ('accepted','unknown') AND price IS NOT NULL ORDER BY id DESC LIMIT 1",(recipient,trip_key)).fetchone()
        return row['price'] if row else None

def last_delivery_status():
    with connect() as db:
        row=db.execute('SELECT * FROM deliveries ORDER BY id DESC LIMIT 1').fetchone()
        return dict(row) if row else None
