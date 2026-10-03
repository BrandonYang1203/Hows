import json
import math
import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import unquote
COLUMNS = [{'key': 'title', 'label': '名稱', 'kind': 'title'}, {'key': 'type', 'label': '類型', 'kind': 'type'}, {'key': 'direction', 'label': '座向', 'kind': 'text'}, {'key': 'price', 'label': '售價', 'kind': 'wan', 'sort': True}, {'key': 'unit_price', 'label': '每坪均價', 'kind': 'unit', 'sort': True}, {'key': 'building_ping', 'label': '總坪數', 'kind': 'ping', 'sort': True}, {'key': 'land_ping', 'label': '土地坪數', 'kind': 'ping', 'sort': True}, {'key': 'floors', 'label': '樓層數', 'kind': 'floors', 'sort': True}, {'key': 'layout', 'label': '格局', 'kind': 'text'}, {'key': 'parking', 'label': '車位', 'kind': 'text'}, {'key': 'age', 'label': '屋齡', 'kind': 'years', 'sort': True}, {'key': 'address', 'label': '地點', 'kind': 'address'}, {'key': 'history', 'label': '歷史買賣', 'kind': 'history'}]
TAIWAN_BOX = (21.8, 25.4, 119.3, 122.1)
IGNORE_COORDS = {(25.05646, 121.52813)}
_TPL = re.compile('\\$\\{|\\{\\{|\\}\\}')
_WS = re.compile('[ \\t\\r\\f\\v\\u00a0\\u3000\\u200b\\ufeff]+')
_EMOJI = re.compile('[🀀-\U0001faff☀-➿⬀-⯿️\u200d]+')

def _clean(s):
    return _WS.sub(' ', s or '').strip()

def _num(s):
    try:
        return float(str(s).replace(',', '').strip())
    except (TypeError, ValueError):
        return None

def _km(a_lat, a_lng, b_lat, b_lng):
    r = 6371.0
    p1, p2 = (math.radians(a_lat), math.radians(b_lat))
    dp, dl = (p2 - p1, math.radians(b_lng - a_lng))
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))

class _Page(HTMLParser):
    SKIP = {'style', 'noscript', 'template', 'svg'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.skip = 0
        self.script = None
        self.scripts = []
        self.meta = {}
        self.title = ''
        self._title = None
        self.h1_depth = 0
        self._h1 = []
        self.h1 = []
        self.a_stack = []
        self.links = []
        self.iframes = []
        self.geo_attrs = []
        self.canonical = ''

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): v or '' for k, v in attrs}
        if tag == 'script':
            self.script = [a.get('type', '').lower(), []]
            return
        if tag in self.SKIP:
            self.skip += 1
            return
        if tag == 'meta':
            key = (a.get('property') or a.get('name') or a.get('itemprop') or '').lower()
            if key and 'content' in a:
                self.meta.setdefault(key, a['content'])
        elif tag == 'link' and 'canonical' in a.get('rel', '').lower():
            self.canonical = a.get('href', '')
        elif tag == 'title':
            self._title = []
        elif tag == 'h1':
            self.h1_depth += 1
        elif tag == 'a':
            self.a_stack.append([a.get('href', ''), []])
        elif tag == 'iframe':
            self.iframes.append(a)
        for k, v in a.items():
            if k.startswith('data-') and v and re.search('lat|lng|lon|center|position|location', k):
                self.geo_attrs.append((k, v))
        self.out.append('\n')

    def handle_endtag(self, tag):
        if tag == 'script':
            if self.script is not None:
                self.scripts.append((self.script[0], ''.join(self.script[1])))
            self.script = None
            return
        if tag in self.SKIP:
            if self.skip:
                self.skip -= 1
            return
        if tag == 'title' and self._title is not None:
            self.title = _clean(''.join(self._title))
            self._title = None
        elif tag == 'h1' and self.h1_depth:
            self.h1_depth -= 1
            if not self.h1_depth:
                t = _clean(''.join(self._h1))
                self._h1 = []
                if t:
                    self.h1.append(t)
        elif tag == 'a' and self.a_stack:
            href, buf = self.a_stack.pop()
            self.links.append((href, _clean(''.join(buf))))
        self.out.append('\n')

    def handle_data(self, data):
        if self.script is not None:
            self.script[1].append(data)
            return
        if self.skip:
            return
        if self._title is not None:
            self._title.append(data)
            return
        if self.h1_depth:
            self._h1.append(data)
        for item in self.a_stack:
            item[1].append(data)
        self.out.append(data)
_UNIT_ONLY = re.compile('^(萬元|萬|億|坪|年|元|元/月|萬/坪|萬元/坪|樓|F|層|%|公尺|米|㎡|平方公尺)$')

def _merge(lines):
    lines = list(lines)
    res = []
    i, n = (0, len(lines))
    while i < n:
        ln = lines[i]
        if ln in ('：', ':') and res and (i + 1 < n):
            res[-1] = res[-1] + '：' + lines[i + 1]
            i += 2
            continue
        if _UNIT_ONLY.match(ln) and res and re.search('\\d$', res[-1]):
            res[-1] += ln
            i += 1
            continue
        if ln in ('總', '約') and i + 1 < n and re.match('^[\\d.,]', lines[i + 1]):
            lines[i + 1] = ln + lines[i + 1]
            i += 1
            continue
        res.append(ln)
        i += 1
    return res
_BULLET = re.compile('^(?:[*•·●▪◦]|[-–](?=\\s)|\\d{1,2}[.、)](?=\\s))\\s*')

def _to_lines(raw):
    out = []
    for ln in raw.split('\n'):
        ln = _BULLET.sub('', _clean(ln))
        if ln:
            out.append(ln)
    return _merge(out)

def text_to_html(text, url=''):
    import html as _h
    body = '\n'.join((f'<div>{_h.escape(ln)}</div>' for ln in (text or '').splitlines()))
    return f'<!doctype html><html><head><meta charset="utf-8"><meta name="x-src" content="paste"><link rel="canonical" href="{_h.escape(url or '', quote=True)}"></head><body>\n{body}\n</body></html>'

def find_canonical(html_text):
    pats = ['<link[^>]+rel=["\\\']canonical["\\\'][^>]+href=["\\\']([^"\\\']+)', '<link[^>]+href=["\\\']([^"\\\']+)["\\\'][^>]+rel=["\\\']canonical', '<meta[^>]+property=["\\\']og:url["\\\'][^>]+content=["\\\']([^"\\\']+)', '<meta[^>]+content=["\\\']([^"\\\']+)["\\\'][^>]+property=["\\\']og:url', 'saved from url=\\(\\d+\\)(https?://\\S+?)\\s*-->']
    for p in pats:
        m = re.search(p, html_text[:400000], re.I)
        if m and m.group(1).startswith('http'):
            return m.group(1)
    return ''

def v_type(s, strict=False):
    if _TPL.search(s) or len(s) > 24:
        return None
    if re.search('透天|別墅|店面|店住|公寓|華廈|大樓|電梯|住宅|套房|住辦|辦公|廠房|農舍|樓中樓', s):
        return s.strip()
    return None

def v_usage(s, strict=False):
    if _TPL.search(s) or len(s) > 20:
        return None
    if re.search('住|商|工業|辦公|農|見使用執照|見謄本', s):
        return s.strip()
    return None

def v_direction(s, strict=False):
    if _TPL.search(s) or len(s) > 14 or re.search('\\d', s):
        return None
    if not re.search('[東西南北]', s) or re.search('[路街站區市巷]|門市|大學|醫院', s):
        return None
    return s.strip()

def v_wan(s, strict=False):
    if _TPL.search(s):
        return None
    m = re.search('([\\d,]+(?:\\.\\d+)?)\\s*億(?:\\s*([\\d,]+)\\s*萬)?', s)
    if m:
        v = _num(m.group(1)) * 10000 + (_num(m.group(2)) or 0 if m.group(2) else 0)
        return v if 50 <= v <= 500000 else None
    m = re.search('([\\d,]+(?:\\.\\d+)?)\\s*萬(?!\\s*/)(?:元)?', s)
    if m:
        v = _num(m.group(1))
        if v and 50 <= v <= 500000:
            return v
    return None

def v_unit(s, strict=False):
    if _TPL.search(s):
        return None
    m = re.search('([\\d,]+(?:\\.\\d+)?)\\s*萬(?:元)?\\s*/\\s*坪', s)
    if not m and (not strict):
        m = re.match('^\\s*([\\d,]+(?:\\.\\d+)?)\\s*萬(?:元)?\\s*$', s)
    if m:
        v = _num(m.group(1))
        if v and 1 <= v <= 1000:
            return v
    return None

def v_ping(s, strict=False):
    if _TPL.search(s):
        return None
    m = re.search('([\\d,]+(?:\\.\\d+)?)\\s*坪', s)
    if m:
        v = _num(m.group(1))
        if v and 0.5 <= v <= 5000:
            return v
    m = re.search('([\\d,]+(?:\\.\\d+)?)\\s*(?:㎡|m²|平方公尺|平方米)', s)
    if m:
        v = _num(m.group(1))
        if v:
            return round(v / 3.30579, 2)
    return None

def v_floors(s, strict=False):
    if _TPL.search(s) or len(s) > 30:
        return None
    t = s.replace('Ｆ', 'F')
    if not re.search('\\d', t) or not re.search('[Ff樓層]|整棟|全', t):
        return None
    if re.search('房|廳|衛|坪|萬|公尺|米', t):
        return None
    return s.strip()

def v_layout(s, strict=False):
    if _TPL.search(s) or len(s) > 24:
        return None
    return s.strip() if re.search('\\d+\\s*房|開放式', s) else None
_PARK_OK = re.compile('無|有|平面|機械|坡道|車庫|升降|塔式|子母|含|自有|室內|戶外|庭院|門前|騎樓|停')
_PARK_STRICT = re.compile('^(無|有|平面|機械|坡道|車庫|升降|塔式|子母|自有|含)')

def v_parking(s, strict=False):
    if _TPL.search(s) or len(s) > 30 or '？' in s or ('?' in s):
        return None
    if re.fullmatch('[\\d.,\\s]+', s):
        return None
    if not (_PARK_STRICT if strict else _PARK_OK).search(s):
        return None
    s = s.strip()
    return '無' if s in ('無車位', '無停車位', '沒有', '無') else s

def v_age(s, strict=False):
    if _TPL.search(s) or len(s) > 20:
        return None
    if '預售' in s:
        return '預售屋'
    if '新成屋' in s:
        return '新成屋'
    m = re.search('([\\d.]+)\\s*年', s)
    if not m:
        return None
    v = _num(m.group(1))
    if v is None:
        return None
    year = datetime.now().year
    if '民國' in s and 30 <= v <= 200:
        return float(max(0, year - (v + 1911)))
    if 1900 <= v <= 2100:
        return float(max(0, year - v))
    if 0 <= v <= 100:
        return v
    return None

def v_address(s, strict=False):
    if _TPL.search(s) or 'http' in s:
        return None
    s = s.strip()
    if not 3 <= len(s) <= 40:
        return None
    if not re.search('[路街道段巷弄號]|[市區鄉鎮]', s):
        return None
    if re.search('距|公尺|導航|看周邊|電話|LINE|可停|附近', s):
        return None
    return s

def v_short(s, strict=False):
    if _TPL.search(s) or len(s) > 20:
        return None
    return s.strip() if re.search('\\d', s) else None
RULES = [('type', ['型態', '建物型態', '房屋型態', '房屋類型', '物件類型', '類型'], v_type), ('usage', ['法定用途', '謄本用途', '用途'], v_usage), ('direction', ['朝向', '座向', '坐向', '大門朝向', '方位'], v_direction), ('price', ['總價', '售價', '開價'], v_wan), ('unit_price', ['單價', '每坪單價', '平均單價'], v_unit), ('building_ping', ['權狀坪數', '建物登記', '登記坪數', '建物坪數', '總建坪', '總坪數', '總建', '建坪'], v_ping), ('main_ping', ['主建物', '主建'], v_ping), ('land_ping', ['土地坪數', '土地登記', '土地面積', '土地持分', '持分坪數', '地坪'], v_ping), ('floors', ['樓層/樓高', '樓別/樓高', '樓層/總樓層', '樓層', '樓高', '總樓層'], v_floors), ('layout', ['格局', '房型'], v_layout), ('parking', ['車位', '停車位', '車位類型', '車位資訊'], v_parking), ('age', ['屋齡', '建築年份', '完工年份', '建成年份'], v_age), ('address', ['地址', '物件地址', '地點', '位置'], v_address), ('road_width', ['臨路路寬', '面前路寬', '臨路寬度', '路寬'], v_short)]
ALL_LABELS = {a for _, al, _ in RULES for a in al} | {'管理費', '帶租約', '租約', '現況', '裝潢程度', '裝潢', '公設比', '隔間', '登記', '經營狀態', '適合行業', '分類', '附屬建物', '共有部分', '共用部分', '車位坪數', '管理方式', '社區', '屋況', '權狀'}

def _is_label(s):
    return s.strip(' ：:') in ALL_LABELS

def _in(i, ranges):
    return any((a <= i < b for a, b in ranges))

def _find_label(lines, aliases, validator, skip=(), only=None):

    def allowed(i):
        if only is not None:
            return _in(i, only)
        return not _in(i, skip)
    for alias in aliases:
        same = re.compile('^' + re.escape(alias) + '\\s*[：:]\\s*(.+)$')
        nodelim = re.compile('^' + re.escape(alias) + '\\s*(\\S.*)$')
        for i, ln in enumerate(lines):
            if not allowed(i):
                continue
            if ln in (alias, alias + '：', alias + ':'):
                for j in (i + 1, i - 1):
                    if 0 <= j < len(lines) and allowed(j) and (not _is_label(lines[j])):
                        v = validator(lines[j])
                        if v is not None:
                            return (v, j)
                continue
            m = same.match(ln)
            if m:
                v = validator(m.group(1))
                if v is not None:
                    return (v, i)
                continue
            m = nodelim.match(ln)
            if m and len(ln) <= len(alias) + 24:
                v = validator(m.group(1), strict=True)
                if v is not None:
                    return (v, i)
    return (None, None)
_PAIR_PATS = [re.compile('["\']?(?:name|label|key|title|text)["\']?\\s*:\\s*"((?:[^"\\\\]|\\\\.){1,40}?)"\\s*,\\s*["\']?(?:value|val|content|desc)["\']?\\s*:\\s*("(?:[^"\\\\]|\\\\.){0,120}?"|-?\\d+(?:\\.\\d+)?)'), re.compile('["\']?(?:value|val|content)["\']?\\s*:\\s*("(?:[^"\\\\]|\\\\.){0,120}?"|-?\\d+(?:\\.\\d+)?)\\s*,\\s*["\']?(?:name|label|key|title)["\']?\\s*:\\s*"((?:[^"\\\\]|\\\\.){1,40}?)\\"')]

def _jstr(s):
    s = s.strip()
    if s.startswith('"'):
        try:
            return str(json.loads(s))
        except ValueError:
            return s.strip('"')
    return s

def _walk_json(o, pairs, geos, depth=0):
    if depth > 14:
        return
    if isinstance(o, dict):
        low = {str(k).lower(): k for k in o}
        lk = next((low[k] for k in ('name', 'label', 'key', 'title') if k in low), None)
        vk = next((low[k] for k in ('value', 'val', 'content', 'desc') if k in low), None)
        if lk and vk and isinstance(o[lk], str) and isinstance(o[vk], (str, int, float)) and (not isinstance(o[vk], bool)) and (len(o[lk]) <= 20):
            pairs.append((o[lk].strip(), str(o[vk]).strip()))
        la = next((low[k] for k in ('lat', 'latitude') if k in low), None)
        ln = next((low[k] for k in ('lng', 'lon', 'long', 'longitude') if k in low), None)
        if la and ln:
            geos.append((_num(o[la]), _num(o[ln]), 4, 'json'))
        for v in o.values():
            if isinstance(v, (dict, list)):
                _walk_json(v, pairs, geos, depth + 1)
    elif isinstance(o, list):
        for v in o[:500]:
            _walk_json(v, pairs, geos, depth + 1)

def _json_material(scripts, xhr):
    pairs, geos, texts = ([], [], [])
    for typ, txt in scripts:
        if 'ld+json' in typ or not txt.strip():
            continue
        texts.append(txt)
    for item in xhr or []:
        body = item.get('body') if isinstance(item, dict) else None
        if not body:
            continue
        texts.append(body)
        try:
            _walk_json(json.loads(body), pairs, geos)
        except ValueError:
            pass
    for txt in texts:
        for m in _PAIR_PATS[0].finditer(txt):
            pairs.append((_jstr('"' + m.group(1) + '"'), _jstr(m.group(2))))
        for m in _PAIR_PATS[1].finditer(txt):
            pairs.append((_jstr('"' + m.group(2) + '"'), _jstr(m.group(1))))
    return (pairs, geos, texts)

def _pair_lookup(pairs, aliases, validator):
    for alias in aliases:
        for k, v in pairs:
            if k.strip(' ：:') == alias:
                r = validator(_clean(v))
                if r is not None:
                    return r
    return None

def _jsonld(scripts):
    out = {}
    objs = []

    def collect(o):
        if isinstance(o, dict):
            objs.append(o)
            for v in o.values():
                collect(v)
        elif isinstance(o, list):
            for v in o:
                collect(v)
    for typ, txt in scripts:
        if 'ld+json' not in typ:
            continue
        t = txt.strip()
        try:
            data = json.loads(t)
        except ValueError:
            try:
                data = json.loads(re.sub(',\\s*([}\\]])', '\\1', t))
            except ValueError:
                continue
        collect(data)
    for o in objs:
        is_item = any((k in o for k in ('offers', 'floorSize', 'numberOfRooms')))
        if is_item and isinstance(o.get('name'), str) and ('title' not in out):
            out['title'] = o['name']
        addr = o.get('address')
        if isinstance(addr, dict) and isinstance(addr.get('streetAddress'), str) and ('address' not in out):
            out['address'] = addr['streetAddress']
        fs = o.get('floorSize')
        if isinstance(fs, dict) and _num(fs.get('value')) and ('building_ping' not in out):
            v = _num(fs.get('value'))
            unit = str(fs.get('unitCode') or fs.get('unitText') or '')
            out['building_ping'] = round(v / 3.30579, 2) if unit.upper() in ('MTK', 'M2', '㎡') else v
        offers = o.get('offers')
        if isinstance(offers, dict) and _num(offers.get('price')) and ('price' not in out):
            p = _num(offers.get('price'))
            out['price'] = round(p / 10000, 1) if p > 100000 else p
        rooms = o.get('numberOfRooms')
        if _num(rooms) and 'rooms' not in out:
            out['rooms'] = int(_num(rooms))
        geo = o.get('geo')
        if isinstance(geo, dict) and _num(geo.get('latitude')) and _num(geo.get('longitude')):
            out.setdefault('geo', (_num(geo['latitude']), _num(geo['longitude'])))
    return out

def _from_meta(meta):
    d = meta.get('description') or meta.get('og:description') or ''
    out = {}
    m = re.search('總價\\s*([\\d,.]+)\\s*萬', d)
    if m:
        out['price'] = _num(m.group(1))
    m = re.search('面積\\s*([\\d,.]+)\\s*坪', d) or re.search('[、，,]\\s*([\\d.]+)\\s*坪\\s*[、，,]', d)
    if m:
        out['building_ping'] = _num(m.group(1))
    m = re.search('房屋類型為([^，,。]+)', d) or re.search('為(透天厝|別墅|店面|公寓|華廈|電梯大樓)', d)
    if m:
        out['type'] = m.group(1).strip()
    m = re.search('格局為([^，,。]+)', d) or re.search('(\\d+房(?:\\d+廳)?(?:\\d+(?:\\.\\d+)?衛)?)', d)
    if m:
        out['layout'] = m.group(1).strip()
    m = re.search('.*位於([^，,。]{2,30}?)[，,]\\s*總價', d) or re.search('位於([^，,。]{2,30}?)[，,]', d)
    if m:
        out['address'] = m.group(1).strip()
    m = re.match('^\\s*([^\\s：:，,]{2,3}[縣市])([^\\s：:，,]{1,3}?[市區鄉鎮])', d)
    if m:
        out['city'] = m.group(1) + m.group(2)
    return out

def _clean_title(t):
    t = _clean(t)
    t = re.sub('^\\[[^\\]]{1,12}\\]\\s*', '', t)
    for _ in range(3):
        u = re.sub('\\s+[-|]\\s+[^-|｜]{1,40}$', '', t)
        if u == t or len(u) < 4:
            break
        t = u
    if len(t) >= 2 and t[0] == '「' and (t[-1] == '」'):
        t = t[1:-1]
    return t.strip() or None

def _ok_coord(lat, lng):
    if lat is None or lng is None:
        return False
    b = TAIWAN_BOX
    if not (b[0] <= lat <= b[1] and b[2] <= lng <= b[3]):
        return False
    return (round(lat, 5), round(lng, 5)) not in IGNORE_COORDS
_URL_COORD = re.compile('(?:[?&](?:q|ll|sll|center|query|destination|daddr|latlng|loc)=|@)\\s*(-?\\d{1,3}\\.\\d{3,})\\s*(?:,|%2C)\\s*(-?\\d{1,3}\\.\\d{3,})', re.I)
_TXT_LATLNG = re.compile('["\']?(?:lat|latitude)["\']?\\s*[:=]\\s*["\']?(-?\\d{1,3}\\.\\d{3,})["\']?[^{}\\[\\]]{0,160}?["\']?(?:lng|lon|long|longitude)["\']?\\s*[:=]\\s*["\']?(-?\\d{1,3}\\.\\d{3,})', re.I)
_TXT_LNGLAT = re.compile('["\']?(?:lng|lon|long|longitude)["\']?\\s*[:=]\\s*["\']?(-?\\d{1,3}\\.\\d{3,})["\']?[^{}\\[\\]]{0,160}?["\']?(?:lat|latitude)["\']?\\s*[:=]\\s*["\']?(-?\\d{1,3}\\.\\d{3,})', re.I)
_TXT_GMAPS = re.compile('LatLng\\(\\s*(-?\\d{1,3}\\.\\d{3,})\\s*,\\s*(-?\\d{1,3}\\.\\d{3,})\\s*\\)')
_TXT_PAIR = re.compile('(2[1-5]\\.\\d{5,})\\s*,\\s*(1[12]\\d\\.\\d{5,})')

def _coords(page, raw_html, json_geos, json_texts, ld_geo, region):
    cands = []

    def add(lat, lng, w, src):
        lat, lng = (_num(lat), _num(lng))
        if _ok_coord(lat, lng):
            cands.append((lat, lng, w, src))
    if ld_geo:
        add(ld_geo[0], ld_geo[1], 6, 'jsonld')
    for href, _text in page.links:
        for m in _URL_COORD.finditer(unquote(href or '')):
            add(m.group(1), m.group(2), 4, 'link')
    for fr in page.iframes:
        if re.search('-\\s*9{4,}', fr.get('style', '')):
            continue
        for m in _URL_COORD.finditer(unquote(fr.get('src', ''))):
            add(m.group(1), m.group(2), 5, 'iframe')
    lat_attr = [v for k, v in page.geo_attrs if 'lat' in k and 'lng' not in k]
    lng_attr = [v for k, v in page.geo_attrs if re.search('lng|lon', k) and 'lat' not in k]
    for la, lo in zip(lat_attr, lng_attr):
        add(la, lo, 4, 'attr')
    for _k, v in page.geo_attrs:
        m = re.search('(-?\\d{1,3}\\.\\d{3,})\\s*,\\s*(-?\\d{1,3}\\.\\d{3,})', v)
        if m:
            add(m.group(1), m.group(2), 3, 'attr')
    for la, lo, w, src in json_geos:
        add(la, lo, w, src)
    for txt in json_texts:
        for m in _TXT_LATLNG.finditer(txt):
            add(m.group(1), m.group(2), 3, 'script')
        for m in _TXT_LNGLAT.finditer(txt):
            add(m.group(2), m.group(1), 3, 'script')
        for m in _TXT_GMAPS.finditer(txt):
            add(m.group(1), m.group(2), 3, 'script')
    if not cands:
        for m in _TXT_PAIR.finditer(raw_html):
            add(m.group(1), m.group(2), 1, 'text')
    if not cands:
        return (None, [])
    groups = {}
    for lat, lng, w, src in cands:
        key = (round(lat, 4), round(lng, 4))
        g = groups.setdefault(key, {'lat': lat, 'lng': lng, 'score': 0, 'src': set()})
        g['score'] += w
        g['src'].add(src)
    items = list(groups.values())
    if region:
        near = [g for g in items if _km(g['lat'], g['lng'], region[0], region[1]) <= 40]
        if near:
            items = near
    best = max(items, key=lambda g: g['score'])
    return ({'lat': round(best['lat'], 7), 'lng': round(best['lng'], 7)}, [(g['lat'], g['lng'], g['score'], sorted(g['src'])) for g in groups.values()])
_HIST_HEAD = re.compile('^(?:本物件|此物件)?(?:實價登錄比對結果?|實價登錄(?:紀錄|記錄)|歷史成交(?:紀錄|記錄)?|(?:歷史)?成交(?:紀錄|記錄)|(?:歷史)?交易(?:紀錄|記錄)|(?:歷史)?價格(?:紀錄|記錄|波動|異動|變動|走勢)|(?:降價|調價|開價)(?:紀錄|記錄|歷程)|(?:物件)?價格歷程)')
_SECTION_STOP = re.compile('^(?:物件詳情|特色描述|屋況特色|屋況介紹|房屋介紹|房屋資料|周邊|生活機能|地圖|房屋問答|推薦|您可能|相似|看了|熱門|聯絡|基礎資訊|坪數|基本$|格局|屋齡|樓層|附近|社區資訊|房貸|貸款|買房知識|店面價格行情)')
_DATE = re.compile('^(\\d{2,4})\\s*[/.\\-年]\\s*(\\d{1,2})(?:\\s*[/.\\-月]\\s*(\\d{1,2})日?)?(?!\\d)\\s*(.*)$')
_H_PRICE = re.compile('^總?\\s*([\\d,]+(?:\\.\\d+)?)\\s*萬(?:元)?\\s*(?:([降漲跌])\\s*([\\d,]+(?:\\.\\d+)?)\\s*萬)?$')
_H_PRICE_IN = re.compile('([\\d,]+(?:\\.\\d+)?)\\s*萬(?:元)?(?!\\s*/)')
_H_AREA = re.compile('^總?\\s*([\\d.]+)\\s*坪$')
_H_UNIT = re.compile('([\\d.]+)\\s*萬\\s*/\\s*坪')
_H_COUNT = re.compile('歷史交易\\s*(\\d+)\\s*次')

def _date_of(y, m, d=None):
    y, m = (int(y), int(m))
    if y < 1000:
        if not 60 <= y <= 200:
            return None
        y += 1911
    if not (1980 <= y <= 2100 and 1 <= m <= 12):
        return None
    return f'{y}/{m:02d}' + (f'/{int(d):02d}' if d else '')

def _history(lines):
    ranges, items = ([], [])
    i = 0
    while i < len(lines):
        ln = lines[i]
        if len(ln) <= 16 and (not re.search('\\d', ln)) and _HIST_HEAD.match(ln):
            start = i + 1
            j = start
            seen_record = False
            while j < len(lines) and j - start < 150:
                if _SECTION_STOP.match(lines[j]) or (j > start and _HIST_HEAD.match(lines[j]) and (len(lines[j]) <= 16)):
                    break
                if _DATE.match(lines[j]) or _H_PRICE.match(lines[j]):
                    seen_record = True
                j += 1
            ranges.append((i, j))
            items.extend(_parse_records(lines[start:j], ln))
            i = j
        else:
            i += 1
    seen, uniq = (set(), [])
    for it in items:
        key = (it.get('date'), it.get('price'), it.get('text'))
        if key not in seen:
            seen.add(key)
            uniq.append(it)
    uniq.sort(key=lambda r: r.get('date') or '', reverse=True)
    return (ranges, uniq[:15])

def _parse_records(sec, head):
    kind = '成交' if re.search('實價|成交|交易', head) else '開價'
    idx_date = next((k for k, l in enumerate(sec) if _date_line(l)), None)
    idx_price = next((k for k, l in enumerate(sec) if _H_PRICE.match(l)), None)
    details_first = idx_date is not None and idx_price is not None and (idx_price < idx_date)
    out, cur = ([], {})

    def close(c):
        if c.get('date') and (c.get('price') or c.get('unit')):
            c.setdefault('kind', kind)
            out.append(c)
    for l in sec:
        dl = _date_line(l)
        if dl:
            date, rest = dl
            price_m = _H_PRICE_IN.search(rest)
            addr = _clean(_H_PRICE_IN.sub('', rest)) if rest else ''
            addr = re.sub('^(?:降價|調價|開價|成交)\\s*(?:至|為)?', '', addr).strip(' ：:-')
            if details_first:
                cur.update(date=date)
                if addr:
                    cur['text'] = addr[:30]
                if price_m and 'price' not in cur:
                    cur['price'] = _num(price_m.group(1))
                close(cur)
                cur = {}
            else:
                close(cur)
                cur = {'date': date}
                if addr:
                    cur['text'] = addr[:30]
                if price_m:
                    cur['price'] = _num(price_m.group(1))
            continue
        m = _H_PRICE.match(l)
        if m:
            if 'price' not in cur:
                cur['price'] = _num(m.group(1))
                if m.group(2):
                    cur['change'] = f'{m.group(2)}{m.group(3)}萬'
            continue
        m = _H_AREA.match(l)
        if m:
            cur['area'] = _num(m.group(1))
            continue
        m = _H_UNIT.search(l)
        if m:
            cur['unit'] = _num(m.group(1))
            continue
        m = _H_COUNT.search(l)
        if m:
            cur['note'] = f'歷史交易{m.group(1)}次'
    if not details_first:
        close(cur)
    return out

def _date_line(l):
    m = _DATE.match(l)
    if not m:
        return None
    date = _date_of(m.group(1), m.group(2), m.group(3))
    return (date, m.group(4)) if date else None
_DESC_HEAD = re.compile('^(?:屋況特色|屋況介紹|特色描述|物件特色|房屋特色|屋主自述|物件說明)')
_DESC_STOP = re.compile('^(?:店面價格行情|價格行情|周邊配套|周邊|地圖|房屋問答|買房知識|推薦|您可能|行業資質|查看更多|檢舉|物件詳情|實價登錄)')

def _desc_ranges(lines):
    out = []
    for i, ln in enumerate(lines):
        if len(ln) <= 10 and _DESC_HEAD.match(ln):
            j = i + 1
            while j < len(lines) and j - i < 90 and (not _DESC_STOP.match(lines[j])):
                j += 1
            out.append((i + 1, j))
    return out

def _desc_hints(text):
    hints = {}
    t = _EMOJI.sub(' ', text)
    m = re.search('(?:地坪|土地(?:坪數|面積|登記)?)\\s*(?:約|為|有|：|:)?\\s*([\\d.]+)\\s*坪', t)
    if m and _num(m.group(1)):
        hints['land_ping'] = _num(m.group(1))
    m = re.search('坐\\s*([東西南北]{1,2})\\s*朝\\s*([東西南北]{1,2})', t)
    if m:
        hints['direction'] = f'坐{m.group(1)}朝{m.group(2)}'
    else:
        m = re.search('(?<!坐)朝\\s*([東西南北]{1,2})(?![路街])', t)
        if m:
            hints['direction'] = '朝' + m.group(1)
    for clause in re.split('[，,。、！!；;\\n]|\\s{2,}', t):
        c = clause.strip(' ✔✓★☆●○◆◇■□▶►-—:：*')
        if re.search('車庫|車位|停車', c) and '？' not in c and ('?' not in c) and (2 <= len(c) <= 26):
            hints['parking'] = c
            break
    m = re.search('屋齡\\s*(?:約|僅|才)?\\s*([\\d.]+)\\s*年', t)
    if m:
        hints['age'] = _num(m.group(1))
    m = re.search('面寬\\s*(?:約|有|達)?\\s*([\\d.]+)\\s*(?:米|公尺|m)', t, re.I)
    if m:
        hints['frontage'] = m.group(1) + ' 米'
    return hints
_ADDR_LINE = re.compile('^(?:\\S{2}[縣市])?\\S{2}[市區鄉鎮][^\\s，,。:：]{1,20}?[路街道段巷弄號]$')
_GONE = re.compile('物件已(?:下架|關閉|成交|售出|刪除)|此物件(?:已|不存在)|找不到(?:此|該|這個)?物件|已下架|已成交|頁面不存在')

def norm_floors(raw):
    if not raw:
        return None
    t = raw.replace('Ｆ', 'F').replace(' ', '')
    n = None
    m = re.search('/(\\d+)(?:F|樓|層)?', t)
    if m:
        n = int(m.group(1))
    else:
        nums = [int(x) for x in re.findall('(\\d+)(?=F|樓|層)', t)]
        if nums:
            n = max(nums)
    return {'n': n, 'raw': raw, 'b': bool(re.search('B\\d|地下', t, re.I))}

def parse(html_text, xhr=None, url='', region=None, debug=False):
    page = _Page()
    try:
        page.feed(html_text or '')
        page.close()
    except Exception:
        pass
    lines = _to_lines(''.join(page.out))
    meta = page.meta
    ld = _jsonld(page.scripts)
    mt = _from_meta(meta)
    pairs, json_geos, json_texts = _json_material(page.scripts, xhr)
    hist_ranges, history = _history(lines)
    desc_ranges = _desc_ranges(lines)
    skip = hist_ranges + desc_ranges
    F, S, hits = ({}, {}, {})

    def put(key, val, src):
        if val is not None and val != '' and (key not in F):
            F[key] = val
            S[key] = src
    h1 = next((h for h in page.h1 if not _TPL.search(h) and len(h) >= 4), None)
    for t, src in ((ld.get('title'), 'jsonld'), (h1, 'h1'), (page.title, 'title'), (meta.get('og:title'), 'meta')):
        if t and (not _TPL.search(t)):
            put('title', _clean_title(t), src)
    for key, aliases, validator in RULES:
        v = _pair_lookup(pairs, aliases, validator)
        if v is not None:
            put(key, v, 'json')
            continue
        v, at = _find_label(lines, aliases, validator, skip=skip)
        if v is not None:
            put(key, v, 'label')
            hits[key] = at
    put('price', mt.get('price'), 'meta')
    put('price', ld.get('price'), 'jsonld')
    put('building_ping', ld.get('building_ping'), 'jsonld')
    put('building_ping', mt.get('building_ping'), 'meta')
    put('type', mt.get('type'), 'meta')
    put('layout', mt.get('layout'), 'meta')
    if ld.get('rooms'):
        put('layout', f'{ld['rooms']}房', 'jsonld')
    put('address', ld.get('address'), 'jsonld')
    for href, text in page.links:
        if _URL_COORD.search(unquote(href or '')) and v_address(text):
            put('address', text, 'link')
            break
    put('address', mt.get('address'), 'meta')
    if 'address' not in F:
        for i, ln in enumerate(lines[:400]):
            if not _in(i, skip) and _ADDR_LINE.match(ln):
                put('address', ln, 'text')
                break
    if 'price' not in F:
        for i, ln in enumerate(lines):
            if not _in(i, skip) and re.match('^[\\d,]+(?:\\.\\d+)?\\s*萬(?:元)?$', ln):
                put('price', v_wan(ln), 'text')
                break
    text_unit = None
    for i, ln in enumerate(lines):
        if not _in(i, skip) and re.match('^[\\d.]+\\s*萬\\s*/\\s*坪$', ln):
            text_unit = v_unit(ln)
            break
    if 'building_ping' not in F:
        for i, ln in enumerate(lines):
            if not _in(i, skip) and re.match('^總\\s*[\\d.]+\\s*坪$', ln):
                put('building_ping', v_ping(ln), 'text')
                break
    if 'building_ping' not in F and 'main_ping' in F:
        put('building_ping', F['main_ping'], 'main')
    calc = round(F['price'] / F['building_ping'], 2) if F.get('price') and F.get('building_ping') else None
    site_unit = F.pop('unit_price', None)
    S.pop('unit_price', None)
    site_unit = site_unit if site_unit is not None else text_unit
    if calc and site_unit and (abs(site_unit - calc) / calc <= 0.02):
        put('unit_price', site_unit, 'label')
    elif calc:
        put('unit_price', calc, 'calc')
        if site_unit:
            F['unit_price_site'] = site_unit
    elif site_unit:
        put('unit_price', site_unit, 'text')
    if 'title' not in F and meta.get('x-src') == 'paste':
        for i, ln in enumerate(lines[:20]):
            if re.search('\\d+\\s*萬|坪|^\\d', ln) or any((ln.startswith(a) for a in ALL_LABELS)):
                break
            if 5 <= len(ln) <= 60 and re.search('[\\u4e00-\\u9fff]', ln) and (not _in(i, skip)):
                put('title', ln, 'text')
                break
    desc_text = '\n'.join((l for i, l in enumerate(lines) if _in(i, desc_ranges)))
    for key, aliases, validator in RULES:
        if key in F or not desc_ranges:
            continue
        v, _ = _find_label(lines, aliases, validator, only=desc_ranges)
        if v is not None:
            put(key, v, 'desc')
    for key, v in _desc_hints(desc_text).items():
        put(key, v, 'desc')
    if isinstance(F.get('floors'), str):
        F['floors'] = norm_floors(F['floors'])
    if F.get('address'):
        a = re.sub('^(?:台灣|臺灣)', '', F['address']).strip()
        if mt.get('city') and (not re.search('[縣市]', a[:4])):
            a = mt['city'] + a
        F['address'] = a
    if history:
        F['history'] = history
        S['history'] = 'label'
    coords, coord_cands = _coords(page, html_text or '', json_geos, json_texts, ld.get('geo'), region)
    text_all = '\n'.join(lines)
    gone = bool(_GONE.search(text_all)) and 'price' not in F
    result = {'fields': F, 'src': S, 'coords': coords, 'gone': gone, 'canonical': page.canonical or meta.get('og:url', '')}
    if debug:
        result['debug'] = {'line_count': len(lines), 'lines': lines[:900], 'hits': {k: (v, lines[v] if v is not None and v < len(lines) else None) for k, v in hits.items()}, 'pairs': pairs[:200], 'coord_candidates': coord_cands, 'history_ranges': [(a, b, lines[a] if a < len(lines) else '') for a, b in hist_ranges], 'desc_ranges': desc_ranges, 'meta': {k: meta[k] for k in ('description', 'og:title', 'og:url') if k in meta}, 'jsonld': {k: v for k, v in ld.items() if k != 'geo'}, 'title_tag': page.title, 'h1': page.h1[:3]}
    return result
