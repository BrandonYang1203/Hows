import base64
import csv
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import extract
TZ = timezone(timedelta(hours=8))
DATA = os.path.join(HERE, 'data')
SNAP = os.path.join(DATA, 'snapshots')
DEBUG = os.path.join(DATA, 'debug')
TRASH = os.path.join(DATA, 'trash')
DB_PATH = os.path.join(DATA, 'db.json.gz')
STATE_PATH = os.path.join(DATA, 'state.json.gz')
GEO_CACHE = os.path.join(DATA, 'geo.json.gz')
LANDMARKS = os.path.join(HERE, 'landmarks.csv')
TEMPLATE = os.path.join(HERE, 'template.html')
DOCS = os.path.join(HERE, 'docs')
REGION_CENTER = (24.827, 121.013)
REGION_ZOOM = 14
DEFAULT_CITY = '新竹縣竹北市'
MIN_INTERVAL = 3.0
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36'
SHEET_CSV_URL = os.environ.get('SHEET_CSV_URL', '').strip()
FORM_URL = os.environ.get('FORM_URL', '').strip()
REPO = os.environ.get('GITHUB_REPOSITORY', '').strip()
PAGE_TITLE = os.environ.get('PAGE_TITLE', '').strip() or '家庭地圖'

def now():
    return datetime.now(TZ).replace(microsecond=0)

def log(msg):
    print(msg, flush=True)

def _open(path, mode):
    if path.endswith('.gz'):
        return gzip.open(path, mode + 't', encoding='utf-8')
    return open(path, mode, encoding='utf-8')

def load_json(path, default):
    try:
        with _open(path, 'r') as f:
            return json.load(f)
    except (OSError, ValueError, EOFError):
        return default

def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp' + ('.gz' if path.endswith('.gz') else '')
    with _open(tmp, 'w') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)

def sha(s, n=12):
    return hashlib.sha1(s.encode('utf-8')).hexdigest()[:n]
URL_IN_TEXT = re.compile('https?://[^\\s<>\\"\'，。]+')

def normalize_url(raw):
    u = (raw or '').strip().strip('<>"\'')
    m = URL_IN_TEXT.search(u)
    if m:
        u = m.group(0)
    if not u:
        return ''
    if not re.match('^https?://', u, re.I):
        u = 'https://' + u
    p = urllib.parse.urlsplit(u)
    host = p.netloc.lower()
    if not host or '.' not in host:
        return ''
    if host.startswith('m.'):
        host = 'www.' + host[2:]
    keep = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if 'id' in k.lower() and (not k.lower().startswith('utm'))]
    return urllib.parse.urlunsplit(('https', host, p.path.rstrip('/') or '/', urllib.parse.urlencode(keep), ''))

def house_id(url):
    return 'h' + sha(url, 10)

def site_of(url):
    host = urllib.parse.urlsplit(url).netloc.lower().split(':')[0]
    parts = [x for x in host.split('.') if x not in ('www', 'm', 'com', 'net', 'org', 'tw', 'co')]
    return parts[-1] if parts else host

def read_sheet():
    if not SHEET_CSV_URL:
        raise SystemExit('還沒設定 SHEET_CSV_URL（試算表發布成 CSV 的網址）')
    req = urllib.request.Request(SHEET_CSV_URL, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode('utf-8-sig', errors='replace')
    rows = list(csv.reader(io.StringIO(raw)))
    if not rows:
        return ([], sha(raw, 16))
    head = [h.strip() for h in rows[0]]

    def col(*keys):
        for i, h in enumerate(head):
            if any((k in h for k in keys)):
                return i
        return None
    idx = {'time': col('時間戳記', '時間', 'Timestamp'), 'url': col('網址', '連結'), 'text': col('內容', '貼上'), 'note': col('備註', '想法', '意見'), 'who': col('你是誰', '名字', '姓名', '誰'), 'geo': col('座標', '位置'), 'title': col('名稱'), 'act': col('這次', '動作', '移除', '刪除')}
    out = []
    for n, row in enumerate(rows[1:], start=2):
        get = lambda k: row[idx[k]].strip() if idx[k] is not None and idx[k] < len(row) else ''
        r = {k: get(k) for k in idx}
        if not any((r[k] for k in ('url', 'text', 'note', 'geo', 'title'))):
            continue
        r['row'] = n
        r['key'] = sha('|'.join([r['time'], r['url'], r['text'][:500], r['note'], r['who'], r['geo'], r['title'], r['act']]), 16)
        out.append(r)
    return (out, sha(raw, 16))

def parse_coord(s):
    if not s:
        return None
    t = urllib.parse.unquote(s)
    m = re.search('@(-?\\d+\\.\\d+),(-?\\d+\\.\\d+)', t) or re.search('(-?\\d{2}\\.\\d{3,})\\s*[,，\\s]\\s*(-?\\d{3}\\.\\d{3,})', t)
    if not m:
        return None
    lat, lng = (float(m.group(1)), float(m.group(2)))
    return (lat, lng) if extract._ok_coord(lat, lng) else None

def load_db():
    db = load_json(DB_PATH, None) or {'version': 1, 'next_no': 1, 'houses': []}
    db.setdefault('houses', [])
    db.setdefault('next_no', 1 + max([h.get('no', 0) for h in db['houses']] or [0]))
    return db

def find(db, hid):
    return next((h for h in db['houses'] if h['id'] == hid), None)

def save_snapshot(hid, html_text, meta):
    base = os.path.join(SNAP, hid)
    stamp = now().strftime('%Y%m%d-%H%M%S')
    d, k = (os.path.join(base, stamp), 1)
    while os.path.exists(d):
        k += 1
        d = os.path.join(base, f'{stamp}_{k}')
    os.makedirs(d)
    with gzip.open(os.path.join(d, 'page.html.gz'), 'wt', encoding='utf-8') as f:
        f.write(html_text)
    save_json(os.path.join(d, 'meta.json.gz'), {**meta, 'saved_at': now().isoformat()})
    return d

def list_snapshots(hid):
    base = os.path.join(SNAP, hid)
    if not os.path.isdir(base):
        return []
    return [os.path.join(base, n) for n in sorted(os.listdir(base)) if os.path.isfile(os.path.join(base, n, 'page.html.gz'))]

def parse_snapshot(d, debug=False):
    with gzip.open(os.path.join(d, 'page.html.gz'), 'rt', encoding='utf-8') as f:
        html_text = f.read()
    meta = load_json(os.path.join(d, 'meta.json.gz'), {})
    r = extract.parse(html_text, url=meta.get('url', ''), region=REGION_CENTER, debug=debug)
    r['fetched_at'] = meta.get('saved_at')
    r['method'] = meta.get('method')
    r['status_code'] = meta.get('status')
    return r

def fix_unit(data, src):
    p, b = (data.get('price'), data.get('building_ping'))
    if not (p and b):
        return
    calc = round(p / b, 2)
    cur = data.get('unit_price')
    if cur is None or src.get('unit_price') == 'calc' or abs(cur - calc) / calc > 0.02:
        if cur is not None and src.get('unit_price') != 'calc':
            data['unit_price_site'] = cur
        data['unit_price'], src['unit_price'] = (calc, 'calc')

def apply_parsed(h, parsed):
    f = parsed['fields']
    has_core = bool(f.get('price') or f.get('building_ping'))
    if not has_core and (h.get('data') or {}).get('price'):
        return
    data, src = (dict(h.get('data') or {}), dict(h.get('src') or {}))
    for k, v in f.items():
        if v not in (None, '', []):
            data[k] = v
            if k in parsed['src']:
                src[k] = parsed['src'][k]
    if 'unit_price_site' not in f and src.get('unit_price') != 'calc':
        data.pop('unit_price_site', None)
    fix_unit(data, src)
    h['data'], h['src'] = (data, src)
    h['gone'] = bool(parsed.get('gone'))
    h['fetched_at'] = parsed.get('fetched_at') or h.get('fetched_at')
    if parsed.get('coords'):
        h['geo'] = {**parsed['coords'], 'src': 'page'}
    if has_core or h.get('data', {}).get('price'):
        h['message'] = None
    else:
        h['message'] = '沒抓到物件資料，請用表單把物件頁內容貼上來補'

def note_price(h, price, when):
    if price is None:
        return
    lg = h.setdefault('price_log', [])
    if not lg or abs(lg[-1]['price'] - price) >= 0.5:
        lg.append({'t': (when or now().isoformat())[:10], 'price': price})
_last = {}

def fetch(url):
    host = urllib.parse.urlsplit(url).netloc
    wait = MIN_INTERVAL - (time.time() - _last.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last[host] = time.time()
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8', 'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.5', 'Accept-Encoding': 'gzip'})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read()
            if (r.headers.get('Content-Encoding') or '').lower() == 'gzip':
                raw = gzip.decompress(raw)
            return (r.status, raw.decode(r.headers.get_content_charset() or 'utf-8', errors='replace'))
    except urllib.error.HTTPError as e:
        return (e.code, '')
    except Exception as e:
        log('  connection failed')
        return (None, '')

def geocode(address):
    if not address:
        return None
    cache = load_json(GEO_CACHE, {})
    key = address.strip()
    if key in cache:
        return cache[key]
    a = re.sub('^(?:台灣|臺灣)', '', key)
    if a.startswith('竹北市'):
        a = '新竹縣' + a
    elif not re.match('^\\S{2}[縣市]', a):
        a = DEFAULT_CITY + a
    queries = [a]
    short = re.sub('\\d+(?:之\\d+)?號.*$', '', a)
    if short != a:
        queries.append(short)
    road = re.sub('\\d+[巷弄].*$', '', short)
    if road != short:
        queries.append(road)
    result, had_error = (None, False)
    for q in queries:
        url = 'https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode({'q': q, 'format': 'jsonv2', 'limit': 1, 'countrycodes': 'tw', 'accept-language': 'zh-TW'})
        req = urllib.request.Request(url, headers={'User-Agent': 'family-map/1.0 (personal use)'})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode('utf-8'))
        except Exception:
            data, had_error = (None, True)
        time.sleep(1.1)
        if data:
            lat, lng = (float(data[0]['lat']), float(data[0]['lon']))
            if extract._km(lat, lng, *REGION_CENTER) <= 60:
                result = {'lat': lat, 'lng': lng}
                break
    if result or not had_error:
        cache[key] = result
        save_json(GEO_CACHE, cache)
    return result

def ensure_geo(h):
    if (h.get('geo') or {}).get('lat') is not None:
        return
    addr = (h.get('data') or {}).get('address')
    g = geocode(addr) if addr else None
    if g:
        h['geo'] = {'lat': g['lat'], 'lng': g['lng'], 'src': 'geocode', 'addr': addr}

def new_house(db, hid, url):
    h = {'id': hid, 'no': db['next_no'], 'url': url, 'site': site_of(url) if url else '貼上', 'links': [url] if url else [], 'aliases': [], 'added_at': now().isoformat(), 'notes': [], 'user': {}, 'price_log': []}
    db['next_no'] += 1
    db['houses'].append(h)
    return h

def ids_of(h):
    return [h['id']] + list(h.get('aliases') or [])

def house_snapshots(h):
    out = []
    for i in ids_of(h):
        out += list_snapshots(i)
    return sorted(out, key=os.path.basename)

def fetch_into(h, url):
    status, html_text = fetch(url)
    h.setdefault('fetched', [])
    if url not in h['fetched']:
        h['fetched'].append(url)
    if status in (404, 410):
        if url == h.get('url'):
            h['removed_at'] = now().isoformat()
        log('  gone')
        return
    if not html_text:
        log(f'  no response {status}')
        return
    save_snapshot(h['id'], html_text, {'url': url, 'method': 'fetch', 'status': status})

def rebuild(h):
    snaps = house_snapshots(h)
    primary = h.get('url', '')
    parsed = []
    for d in snaps:
        try:
            p = parse_snapshot(d)
        except Exception as e:
            log(f'  parse error {type(e).__name__}')
            continue
        meta = load_json(os.path.join(d, 'meta.json.gz'), {})
        p['_url'] = meta.get('url', '')
        p['_primary'] = not p['_url'] or p['_url'] == primary or meta.get('method') == 'paste'
        parsed.append(p)
    useful = [p for p in parsed if p['fields'].get('price') or p['fields'].get('building_ping')]
    if (h.get('geo') or {}).get('src') == 'page':
        h['geo'] = None
    h['data'], h['src'] = ({}, {})
    for p in sorted(useful, key=lambda p: p['_primary']):
        apply_parsed(h, p)
    for p in parsed:
        if p.get('coords') and (not (h.get('geo') or {}).get('lat')):
            h['geo'] = {**p['coords'], 'src': 'page'}
    lg, site_prices = ([], {})
    for p in parsed:
        price = p['fields'].get('price')
        if price is None:
            continue
        site_prices[site_of(p['_url']) if p['_url'] else '貼上'] = price
        if p['_primary'] and (not lg or abs(lg[-1]['price'] - price) >= 0.5):
            lg.append({'t': (p.get('fetched_at') or '')[:10], 'price': price})
    h['price_log'] = lg
    h['site_prices'] = site_prices if len(set(site_prices.values())) > 1 else {}
    g = h.get('geo') or {}
    if g.get('src') == 'geocode' and g.get('addr') != (h.get('data') or {}).get('address'):
        h['geo'] = None
    if (h.get('data') or {}).get('price'):
        h['message'] = None
    elif snaps:
        h['message'] = '網站擋下了或網頁裡沒有物件資料，請用表單把物件頁內容貼上來補'
    else:
        h['message'] = '網站沒有回應，請用表單把物件頁內容貼上來補'
    ensure_geo(h)

def merge_into(db, keep, other):
    keep['aliases'] = list(dict.fromkeys(keep.get('aliases', []) + ids_of(other)))
    keep['links'] = list(dict.fromkeys(keep.get('links', []) + other.get('links', [])))
    keep['fetched'] = list(dict.fromkeys(keep.get('fetched', []) + other.get('fetched', [])))
    keep['notes'] = keep.get('notes', []) + other.get('notes', [])
    ku, ou = (keep.setdefault('user', {}), other.get('user') or {})
    for k, v in ou.items():
        ku.setdefault(k, v)
    keep['pasted'] = list(dict.fromkeys(keep.get('pasted', []) + other.get('pasted', [])))
    db['houses'] = [x for x in db['houses'] if x['id'] != other['id']]
    log(f'  merge {other['no']} -> {keep['no']}')

def process_row(db, r):
    urls = []
    for m in URL_IN_TEXT.finditer(r['url'] + ' ' + (r['text'][:3000] if not r['url'] else '')):
        u = normalize_url(m.group(0))
        if u and u not in urls:
            urls.append(u)
    if not urls and r['url']:
        u = normalize_url(r['url'])
        if u:
            urls.append(u)
    text = r['text']
    if not urls and len(text) < 20:
        m = re.search('(\\d+)\\s*號', r['note'] + ' ' + r['title'])
        h = next((x for x in db['houses'] if m and x['no'] == int(m.group(1))), None)
        if not h:
            log(f'  row {r['row']} skipped')
            return
    else:
        ids = [house_id(u) for u in urls] if urls else ['paste-' + sha(text)]
        matched = [x for x in db['houses'] if set(ids_of(x)) & set(ids)]
        matched.sort(key=lambda x: x['no'])
        if matched:
            h = matched[0]
            for other in matched[1:]:
                merge_into(db, h, other)
        else:
            h = new_house(db, ids[0], urls[0] if urls else '')
        for u, i in zip(urls, ids):
            if u not in h.setdefault('links', []):
                h['links'].append(u)
            if i not in ids_of(h):
                h.setdefault('aliases', []).append(i)
        if not h.get('url') and urls:
            h['url'], h['site'] = (urls[0], site_of(urls[0]))
    if re.search('移除|刪除', r['act']):
        db['houses'] = [x for x in db['houses'] if x['id'] != h['id']]
        dest = os.path.join(TRASH, f'{h['id']}_{now():%Y%m%d-%H%M%S}')
        os.makedirs(dest, exist_ok=True)
        save_json(os.path.join(dest, 'item.json.gz'), h)
        for i in ids_of(h):
            if os.path.isdir(os.path.join(SNAP, i)):
                shutil.move(os.path.join(SNAP, i), os.path.join(dest, i))
        log(f'  removed {h['no']}')
        return
    paste_url = urls[0] if urls else h.get('url', '')
    if len(text) >= 20:
        save_snapshot(h['id'], extract.text_to_html(text, paste_url), {'url': paste_url, 'method': 'paste'})
        h.setdefault('pasted', [])
        if paste_url not in h['pasted']:
            h['pasted'].append(paste_url)
    for u in h.get('links', []):
        if u in (h.get('fetched') or []):
            continue
        if u in (h.get('pasted') or []):
            continue
        fetch_into(h, u)
    if r['note']:
        h.setdefault('notes', []).append({'who': r['who'], 't': r['time'], 'text': r['note'][:1000]})
    c = parse_coord(r['geo'])
    if c:
        h.setdefault('user', {})['lat'], h['user']['lng'] = c
    if r['title']:
        h.setdefault('user', {})['title'] = r['title'][:80]
    rebuild(h)
    f = h.get('data') or {}
    log(f'  #{h['no']} price={('ok' if f.get('price') else 'missing')} geo={('manual' if (h.get('user') or {}).get('lat') else (h.get('geo') or {}).get('src', 'none'))}')

def sync():
    rows, sheet_hash = read_sheet()
    state = load_json(STATE_PATH, {'rows': [], 'sheet_hash': ''})
    done = set(state.get('rows', []))
    todo = [r for r in rows if r['key'] not in done]
    if not todo and os.path.exists(os.path.join(DOCS, 'index.html')) and (state.get('sheet_hash') == sheet_hash):
        log('no new rows')
        return False
    db = load_db()
    for r in todo:
        log(f'row {r['row']}')
        try:
            process_row(db, r)
        except Exception as e:
            log(f'  row {r['row']} error {type(e).__name__}')
        done.add(r['key'])
    save_json(DB_PATH, db)
    save_json(STATE_PATH, {'rows': sorted(done), 'sheet_hash': sheet_hash})
    return True

def reparse_all():
    db = load_db()
    for h in db['houses']:
        rebuild(h)
        log(f'#{h['no']} reparsed')
    save_json(DB_PATH, db)

def refetch_all():
    db = load_db()
    for h in db['houses']:
        for u in h.get('links') or ([h['url']] if h.get('url') else []):
            if u in (h.get('pasted') or []):
                continue
            log(f'#{h['no']} refetch')
            fetch_into(h, u)
        rebuild(h)
    save_json(DB_PATH, db)

def load_landmarks():
    out = []
    try:
        with open(LANDMARKS, encoding='utf-8-sig', newline='') as f:
            for row in csv.reader(f):
                if not row or not row[0].strip() or row[0].strip() in ('名稱', 'name') or row[0].startswith('#'):
                    continue
                try:
                    out.append({'name': row[0].strip(), 'lat': float(row[1]), 'lng': float(row[2])})
                except (IndexError, ValueError):
                    continue
    except OSError:
        pass
    return out

def public(h):
    u = h.get('user') or {}
    g = h.get('geo') or {}
    if u.get('lat') is not None:
        geo = {'lat': u['lat'], 'lng': u['lng'], 'src': 'manual'}
    elif g.get('lat') is not None:
        geo = {'lat': g['lat'], 'lng': g['lng'], 'src': g.get('src', 'page')}
    else:
        geo = {'lat': None, 'lng': None, 'src': 'none'}
    fields, src = (dict(h.get('data') or {}), dict(h.get('src') or {}))
    if u.get('title'):
        fields['title'], src['title'] = (u['title'], 'manual')
    notes = '\n'.join((f'{(n['who'] + '：' if n.get('who') else '')}{n['text']}' for n in h.get('notes') or []))
    return {'id': h['id'], 'no': h['no'], 'url': h.get('url', ''), 'site': h.get('site'), 'fields': fields, 'src': src, 'geo': geo, 'gone': bool(h.get('gone') or h.get('removed_at')), 'price_log': h.get('price_log') or [], 'note': notes, 'fetched_at': h.get('fetched_at'), 'message': h.get('message'), 'links': [{'site': site_of(u), 'url': u} for u in h.get('links') or [] if u != h.get('url')], 'site_prices': h.get('site_prices') or {}, 'debug_url': f'https://github.com/{REPO}/raw/main/data/debug/{h['id']}.txt.gz' if REPO else ''}

def write_debug(h):
    snaps = house_snapshots(h)
    if not snaps:
        return
    p = parse_snapshot(snaps[-1], debug=True)
    dbg = p['debug']
    out = [f'{h['no']}號 {h.get('url', '')}', f'狀態：{h.get('message') or '正常'}', '', '== 最後一份內容的解析結果 ==']
    for c in extract.COLUMNS:
        k = c['key']
        v = p['fields'].get(k)
        hit = dbg['hits'].get(k)
        out.append(f'{c['label']}（{k}）：{('✗' if v is None else json.dumps(v, ensure_ascii=False))}{('  [' + p['src'][k] + ']' if k in p['src'] else '')}{('  ← 第' + str(hit[0]) + '行' if hit else '')}')
    out += [f'座標：{p['coords']}', f'摘要：{dbg['meta']}', '', f'== 內容文字（共 {dbg['line_count']} 行）==']
    out += [f'{i:4d}  {ln}' for i, ln in enumerate(dbg['lines'])]
    os.makedirs(DEBUG, exist_ok=True)
    with gzip.open(os.path.join(DEBUG, f'{h['id']}.txt.gz'), 'wt', encoding='utf-8') as f:
        f.write('\n'.join(out))

def build_page():
    db = load_db()
    for h in db['houses']:
        try:
            write_debug(h)
        except Exception as e:
            log(f'#{h['no']} debug error')
    state = {'houses': [public(h) for h in sorted(db['houses'], key=lambda x: x['no'])], 'landmarks': load_landmarks(), 'columns': extract.COLUMNS, 'region': {'center': list(REGION_CENTER), 'zoom': REGION_ZOOM}, 'exported_at': now().isoformat(), 'form_url': FORM_URL, 'title': PAGE_TITLE}
    with open(TEMPLATE, encoding='utf-8') as f:
        tpl = f.read()
    payload = base64.b64encode(json.dumps(state, ensure_ascii=False).encode('utf-8')).decode('ascii')
    page = tpl.replace('</head>', f'<script>window.__DATA__="{payload}";</script>\n</head>', 1)
    os.makedirs(DOCS, exist_ok=True)
    with open(os.path.join(DOCS, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(page)
    open(os.path.join(DOCS, '.nojekyll'), 'w').close()
    log(f'built {len(state['houses'])}')

def main():
    action = (sys.argv[1] if len(sys.argv) > 1 else '同步表單').strip()
    os.makedirs(SNAP, exist_ok=True)
    if action in ('重新解析全部', 'reparse'):
        if SHEET_CSV_URL:
            sync()
        reparse_all()
    elif action in ('重抓全部網頁', 'refetch'):
        if SHEET_CSV_URL:
            sync()
        refetch_all()
    elif not sync():
        return 0
    build_page()
    return 0
if __name__ == '__main__':
    sys.exit(main())
