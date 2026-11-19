#!/usr/bin/env python3
"""Cat Watch - simple local page. Runs only on THIS computer (127.0.0.1). Nothing is uploaded."""
import atexit, ipaddress, json, mimetypes, os, queue, re, secrets, shutil, subprocess, sys, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote
import setup as S

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = S.CONF_DIR; CONF = S.CONF
OUT = os.environ.get('CATWATCH_OUT', os.path.expanduser('~/cat-watch-snapshots'))
TOKEN = secrets.token_urlsafe(16)
PORT = int(os.environ.get('CATWATCH_PORT', '8765'))
TRIES = os.path.join(HOME, 'tries.json')
# When the cat went missing: the search starts here by default (can be changed on the page).
DEFAULT_MISSING_SINCE = os.environ.get('CATWATCH_MISSING_SINCE', '2026-11-16T16:50')
live, search = {}, {}
# Optional second opinion from Claude Code (only if the `claude` command is installed). Sends the photo to Anthropic.
CLAUDE = shutil.which('claude') or (os.path.expanduser('~/.local/bin/claude') if os.path.exists(os.path.expanduser('~/.local/bin/claude')) else None)
REVIEWS = os.path.join(OUT, 'reviews.json'); rlock = threading.Lock(); askq = queue.Queue(); pending = set()
os.makedirs(HOME, exist_ok=True); os.makedirs(OUT, exist_ok=True)

def private(ip):
    try: return ipaddress.ip_address(ip).is_private
    except Exception: return False

def fill_in(c):
    """Cameras added with an earlier version lack some details: take them from the saved video address."""
    changed = False
    src = c.get('source', '')
    m = re.match(r'rtsp://([^:@/]+):([^@]*)@([^:/]+)', src)
    if m:
        from urllib.parse import unquote as uq
        if not c.get('user'): c['user'] = uq(m.group(1)); changed = True
        if not c.get('pw'): c['pw'] = uq(m.group(2)); changed = True
        if not c.get('ip'): c['ip'] = m.group(3); changed = True
    if not c.get('brand') and c.get('ip'):
        try:
            r = S.identify(c['ip']); c['brand'] = r[0] if r else 'camera'; changed = True
        except Exception: pass
    return changed

def load():
    try: d = json.load(open(CONF))
    except Exception: d = {}
    changed = False
    if 'cams' not in d:                                   # older single-camera file
        d = {'cams': [dict(d, id='cam1')] if d.get('source') else []}; changed = bool(d['cams'])
    for c in d['cams']:
        changed = fill_in(c) or changed
    if 'missing_since' not in d: d['missing_since'] = DEFAULT_MISSING_SINCE; changed = True
    if changed and d['cams']: save(d)
    return d

def save(d):
    S.write_json(CONF, d); os.chmod(CONF, 0o600)

def cam(d, cid): return next((c for c in d['cams'] if c['id'] == cid), None)
def alive(p): return p is not None and p.poll() is None
def kill(p):
    if alive(p):
        p.terminate()
        try: p.wait(5)
        except Exception: p.kill()

def parse_local(v): return time.mktime(time.strptime(v, '%Y-%m-%dT%H:%M'))
def fmt(t): return time.strftime('%a %d %b, %H:%M', time.localtime(t))

# ---------- live watching ----------
def start_live(cid):
    if alive(live.get(cid, {}).get('p')): return
    env = dict(os.environ, CATWATCH_HOME=HOME)
    logf = open(os.path.join(OUT, f'live_{cid}.out'), 'a')
    live[cid] = {'p': subprocess.Popen([sys.executable, os.path.join(HERE, 'agent.py'), '--out', OUT, '--cam', cid], cwd=HERE, env=env, stdout=logf, stderr=logf), 'since': time.time()}
    logf.close()

def stop_live(cid):
    kill(live.get(cid, {}).get('p')); live.pop(cid, None)

# ---------- searching past recordings ----------
def sfile(cid): return os.path.join(OUT, f'search_{cid}.json')

def start_search(cid, frm, to, auto=False):
    stop_search(cid, note=False)
    env = dict(os.environ, CATWATCH_HOME=HOME)
    S.write_json(sfile(cid), {'status': 'running', 'from': frm, 'to': to, 'pos': frm, 'found': 0, 'msg': 'Starting...',
               'auto': auto, 'activity': [[time.time(), ('Keeping up: checking new recordings ' if auto else 'Search started: ') + f'{fmt(frm)} to {fmt(to)}']]})
    logf = open(os.path.join(OUT, f'search_{cid}.out'), 'a')
    search[cid] = subprocess.Popen([sys.executable, os.path.join(HERE, 'agent.py'), '--out', OUT, '--cam', cid, '--from', str(frm), '--to', str(to)],
                                   cwd=HERE, env=env, stdout=logf, stderr=logf)
    logf.close()

def stop_search(cid, note=True):
    p = search.pop(cid, None)
    if alive(p):
        kill(p)
        if note:
            try:
                d = json.load(open(sfile(cid))); d['status'] = 'stopped'
                d['activity'] = (d.get('activity', []) + [[time.time(), 'Stopped by you']])[-40:]
                S.write_json(sfile(cid), d)
            except Exception: pass

def search_info(cid):
    try: d = json.load(open(sfile(cid)))
    except Exception: return None
    p = search.get(cid)
    if d.get('status') == 'running' and not alive(p):
        d['status'] = 'error' if (p is not None and p.returncode not in (0, None, -15)) else 'stopped'
        if d['status'] == 'error': d['msg'] = 'The search stopped unexpectedly.'
    return d


# ---------- keeping up: carry on from where the last search reached ----------
AUTO_EVERY = int(os.environ.get('CATWATCH_AUTO_EVERY', '600'))   # catch up once it is this many seconds behind
EDGE = 120                                                        # leave the last 2 minutes: still being recorded
AUTO_ON = os.environ.get('CATWATCH_AUTO', '1') != '0'
CFG_LOCK = threading.Lock()
paused, last_auto = set(), {}

def searchable(x):
    b = x.get('brand') or ''
    return bool(x.get('pw')) and ('VIGI' in b or 'Hikvision' in b or ('Tapo' in b and x.get('cloud_pw')))

CHECKED = os.path.join(HOME, 'checked.json')   # how far each camera has been checked, kept apart from the camera settings

def ckey(x): return f"{x['id']}@{x.get('ip', '')}"   # a different camera added later under the same id starts fresh

def checked_marks():
    try: return json.load(open(CHECKED))
    except Exception: return {}

def update_checked():
    """Moves each camera's 'checked up to' mark forward, but only over time checked without a hole since he went missing."""
    with CFG_LOCK:
        c = load(); marks = checked_marks(); changed = False
        start = parse_local(c['missing_since'])
        for x in c['cams']:
            d = search_info(x['id'])
            if not d or 'from' not in d or 'pos' not in d: continue
            base = marks.get(ckey(x)) or start
            if d['from'] > base + 120: continue           # this search began after a stretch nobody has checked
            reach = d['pos']
            if d.get('gap') is not None: reach = min(reach, d['gap'])
            if reach > base + 1:
                marks[ckey(x)] = reach; changed = True
        if changed: S.write_json(CHECKED, marks)
        for x in c['cams']: x['checked_until'] = marks.get(ckey(x))
        return c

def auto_loop():
    while True:
        try:
            c = update_checked()
            if AUTO_ON and c.get('auto', True):
                start = parse_local(c['missing_since']); now = time.time()
                for x in c['cams']:
                    cid = x['id']
                    if cid in paused or not searchable(x) or alive(search.get(cid)): continue
                    base = max(x.get('checked_until') or start, start)
                    target = now - EDGE
                    d = search_info(cid) or {}
                    gapmin = int(os.environ.get('CATWATCH_AUTO_WAIT', '300'))
                    wait = gapmin * 3 if d.get('status') == 'error' else gapmin   # do not hammer a camera that just failed
                    first_time = cid not in last_auto
                    if target - base < (60 if first_time else AUTO_EVERY): continue
                    if now - last_auto.get(cid, 0) < wait: continue
                    last_auto[cid] = now
                    start_search(cid, base, target, auto=True)
        except Exception as ex:
            print('auto check:', repr(ex)[:200], flush=True)
        time.sleep(int(os.environ.get('CATWATCH_AUTO_TICK', '30')))

def stop_all():
    for c in list(live): stop_live(c)
    for c in list(search): stop_search(c)
atexit.register(stop_all)

# ---------- Claude second opinion ----------
PROMPT = ('You are checking a home CCTV frame for a missing GOLDEN British Shorthair cat: stocky, round face, golden or cream coat. '
          'At night the camera is infrared, so a golden coat looks pale or white. Use the Read tool to look at the full frame {full} '
          '(the coloured box is what the detector flagged) and the close-up {crop}. Reply with ONLY one line of JSON and nothing else: '
          '{{"verdict": one of "could_be_him", "other_cat", "not_a_cat", "unsure", "what": "2-5 words naming what is in the box", '
          '"reason": "one short sentence"}}')
def reviews():
    with rlock:
        try: return json.load(open(REVIEWS))
        except Exception: return {}
def put_review(f, r):
    with rlock:
        try: d = json.load(open(REVIEWS))
        except Exception: d = {}
        d[f] = r; S.write_json(REVIEWS, d)
def ask_worker():
    while True:
        f = askq.get()
        try:
            day = re.search(r'(\d{4}-\d{2}-\d{2})_\d{6}', f).group(1)
            full = os.path.join(day, f); crop = os.path.join(day, f.replace('.jpg', '_crop.jpg'))
            r = subprocess.run([CLAUDE, '-p', PROMPT.format(full=full, crop=crop), '--allowedTools', 'Read', '--model', 'sonnet', '--output-format', 'json'],
                               cwd=OUT, capture_output=True, text=True, timeout=180)
            res = json.loads(r.stdout).get('result', '')
            m = re.search(r'\{.*\}', res, re.S)
            v = json.loads(m.group(0)) if m else {'verdict': 'unsure', 'what': '', 'reason': res[:160]}
            v['t'] = time.time(); put_review(f, v)
        except Exception as ex:
            put_review(f, {'verdict': 'error', 'what': '', 'reason': 'Could not get an answer from Claude: ' + repr(ex)[:100], 't': time.time()})
        finally:
            pending.discard(f)
if CLAUDE: threading.Thread(target=ask_worker, daemon=True).start()

def ask(files):
    if not CLAUDE: return {'ok': False, 'msg': 'Claude Code is not installed on this computer.'}
    done = reviews(); n = 0
    for f in files:
        mday = re.search(r'(\d{4}-\d{2}-\d{2})_\d{6}', f or '')
        if not mday or '/' in f or '..' in f or not os.path.isfile(os.path.join(OUT, mday.group(1), f)): continue
        if f not in pending and (f not in done or done[f].get('verdict') == 'error'):
            pending.add(f); askq.put(f); n += 1
    return {'ok': True, 'queued': n}

# ---------- video files from any camera ----------
UPLOADS = os.path.join(OUT, '_uploads'); os.makedirs(UPLOADS, exist_ok=True)
def safe_file(n):
    n = os.path.basename(n or '').strip()
    n = re.sub(r'[^A-Za-z0-9._ -]+', '_', n)[:120]
    return n if n and not n.startswith('.') else None
def uploads():
    out = []
    for f in sorted(os.listdir(UPLOADS)):
        p = os.path.join(UPLOADS, f)
        if os.path.isfile(p) and not f.endswith('.part'): out.append({'name': f, 'size': os.path.getsize(p)})
    return out
def check_files(d):
    names = [n for n in (d.get('names') or []) if safe_file(n) == n and os.path.isfile(os.path.join(UPLOADS, n))]
    if not names: return {'ok': False, 'msg': 'Add some video files first.'}
    starts = []
    for n in names:
        v = (d.get('starts') or {}).get(n)
        try: starts.append(str(parse_local(v)) if v else 'auto')
        except Exception: starts.append('auto')
    stop_search('files', note=False)
    env = dict(os.environ, CATWATCH_HOME=HOME)
    S.write_json(sfile('files'), {'status': 'running', 'from': time.time(), 'to': time.time(), 'pos': time.time(), 'found': 0, 'msg': 'Starting...',
                                  'activity': [[time.time(), f'Checking {len(names)} video file' + ('s' if len(names) != 1 else '')]]})
    logf = open(os.path.join(OUT, 'search_files.out'), 'a')
    search['files'] = subprocess.Popen([sys.executable, os.path.join(HERE, 'agent.py'), '--out', OUT, '--cam', 'files', '--name', 'Video files',
                                        '--files'] + [os.path.join(UPLOADS, n) for n in names] + ['--starts'] + starts, cwd=HERE, env=env, stdout=logf, stderr=logf)
    logf.close()
    return {'ok': True, 'msg': f'Checking {len(names)} video file' + ('s' if len(names) != 1 else '') + '. Results appear below as they are found.'}

# ---------- photos ----------
SNAP_RE = re.compile(r'^(?P<cam>.*?)_(?:(?P<mode>LIVE|REC|FILE)(?P<kind>[MD])?_)?(?P<ts>\d{4}-\d{2}-\d{2}_\d{6})_(?P<conf>[\d.]+)_crop\.jpg$')
def snapshots(limit=5000):
    items = []
    for d in sorted(os.listdir(OUT), reverse=True):
        dp = os.path.join(OUT, d)
        if not os.path.isdir(dp): continue
        for f in os.listdir(dp):
            m = SNAP_RE.match(f)
            if not m: continue
            try: tt = time.mktime(time.strptime(m.group('ts'), '%Y-%m-%d_%H%M%S'))
            except Exception: tt = os.path.getmtime(os.path.join(dp, f))
            full = f.replace('_crop.jpg', '.jpg')
            items.append({'review': None, 't': tt, 'camkey': m.group('cam'), 'cam': m.group('cam').replace('-', ' '), 'mode': {'REC': 'past', 'FILE': 'file'}.get(m.group('mode'), 'live'), 'kind': {'M': 'movement', 'D': 'dog'}.get(m.group('kind') or '', 'cat'),
                          'conf': float(m.group('conf')), 'crop': f'/snap/{d}/{f}?t={TOKEN}', 'full': f'/snap/{d}/{full}?t={TOKEN}', 'file': full})
    items.sort(key=lambda x: -x['t'])
    rv = reviews()
    for x in items:
        x['review'] = rv.get(x['file']) or ({'verdict': 'checking'} if x['file'] in pending else None)
    return items[:limit]

# ---------- setup ----------
def scan(hosts_arg):
    if hosts_arg:
        hosts = [h.strip() for h in hosts_arg.split(',') if private(h.strip())]
    else:
        me = S.local_ip(); base = '.'.join(me.split('.')[:3])
        hosts = [f'{base}.{i}' for i in range(1, 255) if f'{base}.{i}' != me]
        hosts = sorted(set(hosts) | {h for h in S.onvif_discovery(2.0) if private(h)})
    cams = S.scan(hosts); have = {c['ip'] for c in load()['cams']}
    return [{'ip': ip, 'brand': cams[ip][0], 'rtsp': cams[ip][1]['rtsp_port_open'], 'added': ip in have}
            for ip in sorted(cams, key=lambda x: tuple(int(p) for p in x.split('.')))]

def tries_ok(ip):
    try: t = json.load(open(TRIES))
    except Exception: t = {}
    now = time.time(); t[ip] = [x for x in t.get(ip, []) if now - x < 1800]
    return t, len(t[ip]) < 3

def hik_channels(d):
    """Lists the cameras on a Hikvision recorder (or the one camera) so the user can pick which to add."""
    ip = d.get('ip', ''); user = d.get('user', '') or 'admin'; pw = d.get('password', '')
    if not private(ip): return {'ok': False, 'msg': 'That address is not on your home network.'}
    if not pw: return {'ok': False, 'msg': 'Please type the password.'}
    t, ok = tries_ok(ip)
    if not ok: return {'ok': False, 'msg': 'Stopped after 3 wrong tries so the device does not lock you out. Wait 30 minutes and check the password.'}
    try: dev = S.hik_device(f'http://{ip}', user, pw)
    except PermissionError:
        t[ip].append(time.time()); json.dump(t, open(TRIES, 'w'))
        return {'ok': False, 'msg': f'The password was not accepted. {3 - len(t[ip])} tries left.'}
    except Exception as ex:
        return {'ok': False, 'msg': 'Could not ask the device for its cameras: ' + repr(ex)[:80]}
    have = {(x['ip'], int(x.get('channel') or 1)) for x in load()['cams']}
    for ch in dev['channels']: ch['added'] = (ip, ch['ch']) in have
    return {'ok': True, **dev}

def connect(d):
    ip = d.get('ip', ''); user = d.get('user', '') or 'admin'; pw = d.get('password', ''); name = (d.get('name') or 'camera').strip()[:30]
    if not private(ip): return {'ok': False, 'msg': 'That address is not on your home network.'}
    if not pw: return {'ok': False, 'msg': 'Please type the camera password.'}
    t, ok = tries_ok(ip)
    if not ok: return {'ok': False, 'msg': 'Stopped after 3 wrong tries so the camera does not lock you out. Wait 30 minutes and check the password.'}
    try: ch = max(1, min(64, int(d.get('channel') or 1)))
    except Exception: ch = 1
    try: url, why = S.probe_rtsp(ip, user, pw, d.get('brand', ''), ch)
    except Exception as ex: return {'ok': False, 'msg': 'Could not test the camera: ' + repr(ex)[:80]}
    if not url and why == 'timeout':           # no login happened, so this does not count as a wrong try
        return {'ok': False, 'msg': 'The camera did not answer in time. It may be busy; try again in a minute. This did not count as a wrong password.'}
    if not url:
        t[ip].append(time.time()); json.dump(t, open(TRIES, 'w'))
        return {'ok': False, 'msg': f'Could not open the video. The password may be wrong, or the camera has no RTSP account yet. {3 - len(t[ip])} tries left.'}
    hik = 'Hikvision' in d.get('brand', '')
    c = load(); ex = next((x for x in c['cams'] if x['ip'] == ip and (not hik or int(x.get('channel') or 1) == ch)), None)   # a recorder has one entry per channel
    if ex is None:
        n = 1
        while cam(c, f'cam{n}'): n += 1
        ex = {'id': f'cam{n}'}; c['cams'].append(ex)
    ex.update({'name': name, 'ip': ip, 'user': user, 'pw': pw, 'brand': d.get('brand', ''), 'source': url})
    if 'Hikvision' in d.get('brand', ''): ex['channel'] = ch
    if d.get('cloud_pw'): ex['cloud_pw'] = d['cloud_pw']
    save(c)
    return {'ok': True, 'msg': f'Added "{name}".'}

def remove(cid):
    stop_live(cid); stop_search(cid)
    c = load(); c['cams'] = [x for x in c['cams'] if x['id'] != cid]; save(c)
    return {'ok': True}

def do_search(d):
    c = load(); ids = d.get('ids')
    if ids is None: ids = [x['id'] for x in c['cams']]
    chosen = [cam(c, i) for i in ids if cam(c, i)]
    if not chosen: return {'ok': False, 'msg': 'Tick at least one camera to search.'}
    try: frm = parse_local(d.get('from') or c['missing_since'])
    except Exception: return {'ok': False, 'msg': 'Please choose the start time.'}
    to = time.time()
    if d.get('to'):
        try: to = min(parse_local(d['to']), time.time())
        except Exception: return {'ok': False, 'msg': 'The end time is not valid.'}
    if to <= frm: return {'ok': False, 'msg': 'The end time must be after the start time.'}
    if d.get('password'):
        for x in chosen: x['pw'] = d['password']
    save(c)
    notes = []
    ready = []
    for x in chosen:
        b = x.get('brand') or ''
        if 'VIGI' in b or 'Hikvision' in b: ready.append(x)
        elif 'Tapo' in b:
            if x.get('cloud_pw'): ready.append(x)
            else: notes.append(f'{x["name"]}: add the Tapo account password (Cameras section) to search its recordings')
        else: notes.append(f'{x["name"]}: this camera type cannot be searched directly. Download its clips and use Check video files')
    if not ready: return {'ok': False, 'msg': ' · '.join(notes)}
    for x in ready: paused.discard(x['id']); start_search(x['id'], frm, to)
    chosen = ready
    names = ' and '.join(x['name'] for x in chosen)
    msg = f'Searching {names}: {fmt(frm)} to {fmt(to)} ({(to - frm) / 3600:.1f} hours of recordings each).' + (' ' + ' · '.join(notes) if notes else '')
    return {'ok': True, 'msg': msg}

def status():
    c = update_checked(); out = []
    for x in c['cams']:
        lv = live.get(x['id'], {})
        out.append({'id': x['id'], 'key': re.sub(r'[^A-Za-z0-9_-]+', '-', x.get('name', 'camera')).strip('-') or x['id'], 'name': x.get('name', 'camera'), 'ip': x.get('ip'), 'brand': x.get('brand', ''),
                    'live': alive(lv.get('p')), 'live_since': lv.get('since'), 'search': search_info(x['id']),
                    'checked_until': x.get('checked_until'), 'paused': x['id'] in paused, 'searchable': searchable(x),
                    'next_check': max(x.get('checked_until') or 0, parse_local(c['missing_since'])) + EDGE + AUTO_EVERY})
    return {'cams': out, 'missing_since': c['missing_since'], 'auto': AUTO_ON and c.get('auto', True), 'auto_every': AUTO_EVERY, 'claude': bool(CLAUDE), 'checking': len(pending), 'files': search_info('files'), 'uploads': uploads()}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _host_ok(self): return self.headers.get('Host', '').split(':')[0] in ('127.0.0.1', 'localhost')
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
    def _auth(self):
        q = parse_qs(urlparse(self.path).query)
        return self._host_ok() and (self.headers.get('X-Token') == TOKEN or q.get('t', [''])[0] == TOKEN)
    def do_GET(self):
        u = urlparse(self.path)
        if not self._host_ok(): return self._json({'error': 'bad host'}, 403)
        if u.path == '/':
            html = open(os.path.join(HERE, 'ui.html'), encoding='utf8').read().replace('__TOKEN__', TOKEN).encode()
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.send_header('Content-Length', str(len(html)))
            self.send_header('Cache-Control', 'no-store'); self.end_headers(); self.wfile.write(html); return
        if not self._auth(): return self._json({'error': 'forbidden'}, 403)
        if u.path == '/api/status': return self._json(status())
        if u.path == '/api/snapshots': return self._json(snapshots())
        if u.path.startswith('/snap/'):
            parts = unquote(u.path[6:]).split('/')
            if len(parts) != 2 or '..' in parts[0] + parts[1]: return self._json({'error': 'bad path'}, 400)
            p = os.path.join(OUT, parts[0], parts[1])
            if not os.path.isfile(p): return self._json({'error': 'not found'}, 404)
            b = open(p, 'rb').read(); self.send_response(200); self.send_header('Content-Type', mimetypes.guess_type(p)[0] or 'image/jpeg')
            self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b); return
        self._json({'error': 'not found'}, 404)
    def do_POST(self):
        if not self._host_ok() or self.headers.get('X-Token') != TOKEN: return self._json({'error': 'forbidden'}, 403)
        n = int(self.headers.get('Content-Length') or 0)
        if urlparse(self.path).path == '/api/upload':
            name = safe_file(unquote(self.headers.get('X-File-Name', '')))
            if not name: return self._json({'ok': False, 'msg': 'Bad file name.'}, 400)
            dst = os.path.join(UPLOADS, name); part = dst + '.part'; left = n
            with open(part, 'wb') as f:
                while left > 0:
                    b = self.rfile.read(min(left, 1 << 20))
                    if not b: break
                    f.write(b); left -= len(b)
            if left: os.remove(part); return self._json({'ok': False, 'msg': 'Upload interrupted.'})
            os.replace(part, dst)
            return self._json({'ok': True, 'name': name})
        d = json.loads(self.rfile.read(n) or b'{}')
        p = urlparse(self.path).path; c = load(); ids = d.get('ids') or [x['id'] for x in c['cams']]
        if p == '/api/scan': return self._json({'cams': scan(d.get('hosts'))})
        if p == '/api/ask': return self._json(ask(d.get('files') or []))
        if p == '/api/check_files': return self._json(check_files(d))
        if p == '/api/files_stop': stop_search('files'); return self._json({'ok': True})
        if p == '/api/remove_upload':
            nm = safe_file(d.get('name'))
            if nm and os.path.isfile(os.path.join(UPLOADS, nm)): os.remove(os.path.join(UPLOADS, nm))
            return self._json({'ok': True})
        if p == '/api/connect': return self._json(connect(d))
        if p == '/api/hik_channels': return self._json(hik_channels(d))
        if p == '/api/auto':
            with CFG_LOCK:
                cc = load(); cc['auto'] = bool(d.get('on')); save(cc)
            if cc['auto']: paused.clear()
            return self._json({'ok': True})
        if p == '/api/remove': return self._json(remove(d.get('id', '')))
        if p == '/api/search': return self._json(do_search(d))
        if p == '/api/search_stop':
            for i in ids: stop_search(i); paused.add(i)
            return self._json({'ok': True})
        if p == '/api/start':
            if not c['cams']: return self._json({'ok': False, 'msg': 'Add a camera first.'})
            for i in ids: start_live(i)
            return self._json({'ok': True})
        if p == '/api/stop':
            for i in ids: stop_live(i)
            return self._json({'ok': True})
        self._json({'error': 'not found'}, 404)

def main():
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), H)
    threading.Thread(target=auto_loop, daemon=True).start()
    url = f'http://127.0.0.1:{PORT}/'
    print('Cat Watch is open at', url, '(only on this computer). Close this window to stop.', flush=True)
    if not os.environ.get('CATWATCH_NOBROWSER'): threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: pass

if __name__ == '__main__': main()
