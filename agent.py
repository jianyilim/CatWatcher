#!/usr/bin/env python3
"""Cat Watch agent: looks for a cat on ONE camera on YOUR network, entirely on your own computer.
Live mode watches the camera now. History mode (TP-Link VIGI) searches past recordings between two times.
Nothing is uploaded. Snapshots are saved for YOU to look at; you decide whether to share them."""
import argparse, asyncio, datetime, hashlib, json, os, platform, re, ssl, subprocess, sys, time
import numpy as np
from PIL import Image, ImageDraw

def log(out, *a):
    line = datetime.datetime.now().strftime('%d/%m %H:%M:%S ') + ' '.join(str(x) for x in a)
    print(line, flush=True)
    with open(os.path.join(out, 'agent.log'), 'a', encoding='utf8') as f: f.write(line + '\n')

def notify(title, msg):
    if os.environ.get('CATWATCH_QUIET'): return   # tests and background checks stay silent
    try:
        s = platform.system()
        if s == 'Darwin':
            subprocess.run(['osascript', '-e', f'display notification "{msg}" with title "{title}" sound name "Glass"'], capture_output=True)
        elif s == 'Windows':
            ps = ("[reflection.assembly]::loadwithpartialname('System.Windows.Forms')|Out-Null;"
                  "$n=New-Object System.Windows.Forms.NotifyIcon;$n.Icon=[System.Drawing.SystemIcons]::Information;"
                  f"$n.Visible=$true;$n.ShowBalloonTip(8000,'{title}','{msg}',[System.Windows.Forms.ToolTipIcon]::Info);Start-Sleep 9;$n.Dispose()")
            subprocess.Popen(['powershell', '-NoProfile', '-Command', ps])
        else:
            subprocess.run(['notify-send', title, msg], capture_output=True)
    except Exception:
        pass

def device_name():
    try:
        import torch
        if torch.cuda.is_available(): return 'cuda'
        if torch.backends.mps.is_available(): return 'mps'
    except Exception:
        pass
    return 'cpu'

def pick_model(name):
    if name != 'auto': return name
    return 'yolo11x.pt' if device_name() in ('cuda', 'mps') else 'yolo11m.pt'

def write_json(path, obj):
    """Writes a whole file at once. Windows refuses to replace a file another program is reading, so retry briefly."""
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf8') as f: json.dump(obj, f)
    for i in range(20):
        try: os.replace(tmp, path); return
        except PermissionError:
            if i == 19: raise
            time.sleep(0.05)

def plural(n, word): return f'{n} {word}' + ('' if n == 1 else 's')

def safe_name(n, fallback='camera'):
    return re.sub(r'[^A-Za-z0-9_-]+', '-', n).strip('-') or fallback

CHILDREN = []
def _close_children(*_):
    for c in CHILDREN:
        try:
            if c.poll() is None: c.kill()
        except Exception: pass
import atexit, signal
atexit.register(_close_children)
try: signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # stopping runs the clean-up above
except Exception: pass

def frames(source, fps, w=1280, h=720, seek=0):
    cmd = ['ffmpeg', '-v', 'error']
    if source.lower().startswith('rtsp'): cmd += ['-rtsp_transport', 'tcp']
    if seek > 0: cmd += ['-ss', f'{seek:.3f}']
    cmd += ['-i', source, '-vf', f'fps={fps},scale={w}:{h}', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-']
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL); CHILDREN.append(p)
    n = w * h * 3
    try:
        while True:
            b = p.stdout.read(n)
            if len(b) < n: break
            yield np.frombuffer(b, np.uint8).reshape(h, w, 3)
    finally:
        p.kill()

class Detector:
    """Two kinds of result:
    - Cat: the cat/dog detector (YOLO) is confident.
    - Movement: a small, animal-sized thing moves and stays in view for a few frames while no person or bird is seen.
      This catches cats the detector cannot recognise, e.g. half hidden behind a car. Saved separately, to check by eye."""
    def __init__(self, model, dev, out, name, conf=0.30, cooldown=600, always_check=20, live=True, movement=True):
        self.m, self.dev, self.out, self.name = model, dev, out, name
        self.conf, self.cooldown, self.always, self.live, self.movement = conf, cooldown, always_check, live, movement
        self.last_alert = -1e12; self.hits = []; self.found = 0; self.moves = 0; self.mv_spots = []
        self.reset()
    def reset(self):
        self.prev = None; self.active_until = -1e12; self.last_check = -1e12
        self.mbg = None; self.tracks = []; self.block_until = -1e12
    def _movement(self, fr, t):
        import cv2
        g = cv2.resize(cv2.cvtColor(fr, cv2.COLOR_RGB2GRAY), (320, 180), interpolation=cv2.INTER_AREA).astype(np.float32)
        if self.mbg is None: self.mbg = g.copy(); return None
        light = abs(float(g.mean()) - float(self.mbg.mean()))
        mask = (cv2.absdiff(g, self.mbg) > 22).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        cv2.accumulateWeighted(g, self.mbg, 0.05)
        if mask.mean() > 0.15 or light > 4: self.tracks = []; return None   # light change, dawn/dusk or camera switching day/night
        n, lab, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
        cands = [(cent[i], stats[i]) for i in range(1, n) if 30 <= stats[i][4] <= 1200 and stats[i][2] <= 90 and stats[i][3] <= 90]
        for (cx, cy), s in cands:
            tr = next((tr for tr in self.tracks if abs(tr['x'] - cx) < 25 and abs(tr['y'] - cy) < 25), None)
            if tr: tr.update(x=cx, y=cy, hits=tr['hits'] + 1, last=t, box=s)
            else: self.tracks.append({'x': cx, 'y': cy, 'hits': 1, 'first': t, 'last': t, 'box': s})
        self.tracks = [tr for tr in self.tracks if t - tr['last'] <= 6]
        best = max((tr for tr in self.tracks if tr['hits'] >= 5), key=lambda tr: tr['hits'], default=None)
        if best is None: return None
        x, y, w, h = [int(v) for v in best['box'][:4]]
        return (x * 4, y * 4, (x + w) * 4, (y + h) * 4), (best['x'], best['y'])
    def feed(self, fr, t):
        g = np.asarray(Image.fromarray(fr).convert('L').resize((160, 90)), dtype=np.int16)
        moving = self.prev is not None and (np.abs(g - self.prev) > 25).mean() > 0.004
        self.prev = g if self.prev is None else (0.9 * self.prev + 0.1 * g).astype(np.int16)
        mv = self._movement(fr, t) if self.movement else None
        if moving or mv: self.active_until = t + 6
        if not (t < self.active_until or t - self.last_check >= self.always): return
        self.last_check = t
        r = self.m.predict(fr, conf=0.25, imgsz=1280, classes=[0, 14, 15, 16], verbose=False, device=self.dev)[0]
        people = [tuple(float(v) for v in b.xyxy[0].tolist()) for b in r.boxes if int(b.cls[0]) == 0 and float(b.conf[0]) >= 0.3]
        def on_person(b):                         # a crouching person is often mistaken for a cat or dog
            x1, y1, x2, y2 = (float(v) for v in b.xyxy[0].tolist()); area = max(1.0, (x2 - x1) * (y2 - y1))
            for px1, py1, px2, py2 in people:
                iw = max(0.0, min(x2, px2) - max(x1, px1)); ih = max(0.0, min(y2, py2) - max(y1, py1))
                if iw * ih / area >= 0.5: return True
            return False
        cats = [b for b in r.boxes if int(b.cls[0]) in (15, 16) and not on_person(b)]
        if any(int(b.cls[0]) in (0, 14) and float(b.conf[0]) >= 0.35 for b in r.boxes): self.block_until = t + 8   # a person or bird is here
        saved = False
        if cats:
            b = max(cats, key=lambda x: float(x.conf[0]))
            c = float(b.conf[0]); box = tuple(int(v) for v in b.xyxy[0].tolist()); kind = 'cat' if int(b.cls[0]) == 15 else 'dog'
            self.hits = [(tt, cc) for tt, cc in self.hits if 0 <= t - tt < 12] + [(t, c)]
            strong = c >= 0.5 or sum(1 for _, cc in self.hits if cc >= self.conf) >= 2
            if strong and t - self.last_alert >= self.cooldown:
                self.last_alert = t; self.save(fr, box, c, t, kind=kind); saved = True
            if strong: saved = True
        if mv and not saved and t >= self.block_until and t - getattr(self, 'last_mv', -1e12) >= 300:   # at most one movement photo per 5 minutes
            box, (cx, cy) = mv
            self.mv_spots = [(sx, sy, st) for sx, sy, st in self.mv_spots if t - st < 600]
            if not any(abs(sx - cx) < 40 and abs(sy - cy) < 40 for sx, sy, st in self.mv_spots):   # one per spot per 10 minutes
                self.mv_spots.append((cx, cy, t)); self.last_mv = t; self.save(fr, box, 0.0, t, movement=True)
    def save(self, fr, box, c, t, movement=False, kind='cat'):
        when = datetime.datetime.fromtimestamp(t)
        d = os.path.join(self.out, when.strftime('%Y-%m-%d')); os.makedirs(d, exist_ok=True)
        im = Image.fromarray(fr); ImageDraw.Draw(im).rectangle(box, outline=(255, 196, 0) if movement else (255, 0, 0), width=4)
        x1, y1, x2, y2 = box; pad = 120 if movement else 90
        mode = getattr(self, 'mode_tag', None) or ('LIVE' if self.live else 'REC')
        mode += ('M' if movement else 'D' if kind == 'dog' else '')
        # do not save the same visit twice (e.g. when the same hours are searched again)
        stamp_re = re.compile(re.escape(self.name) + r'_' + mode + r'_(\d{4}-\d{2}-\d{2}_\d{6})_[\d.]+\.jpg$')
        for f in os.listdir(d):
            mm = stamp_re.match(f)
            if mm and abs(time.mktime(time.strptime(mm.group(1), '%Y-%m-%d_%H%M%S')) - t) < 60:
                return
        p1 = os.path.join(d, f'{self.name}_{mode}_{when.strftime("%Y-%m-%d_%H%M%S")}_{c:.2f}.jpg'); im.save(p1, quality=90)
        im.crop((max(0, x1 - pad), max(0, y1 - pad), min(im.width, x2 + pad), min(im.height, y2 + pad))).save(p1.replace('.jpg', '_crop.jpg'), quality=92)
        if movement:
            self.moves += 1
            log(self.out, 'movement', when.strftime('%d/%m %H:%M:%S'), p1)
            if getattr(self, 'on_move', None): self.on_move(when)
            return
        self.found += 1
        if getattr(self, 'on_found', None): self.on_found(when, c, kind)
        log(self.out, 'possible cat/dog', when.strftime('%d/%m %H:%M:%S'), f'conf={c:.2f}', p1)
        if self.live: notify('Cat Watch', f'Possible cat on {self.name} - open Cat Watch to check')


def file_start_time(path):
    """When a downloaded clip was recorded: from the file's own metadata, then its name, then its date on disk."""
    try:
        r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format_tags=creation_time', '-of', 'csv=p=0', path], capture_output=True, text=True, timeout=20)
        v = r.stdout.strip()
        if v and not v.startswith('1970'):
            return datetime.datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp(), 'from the file'
    except Exception:
        pass
    m = re.search(r'(20\d{2})[-_]?(\d{2})[-_]?(\d{2})[-_ T]?(\d{2})[-_:]?(\d{2})[-_:]?(\d{2})', os.path.basename(path))
    if m:
        try: return time.mktime(time.strptime(''.join(m.groups()), '%Y%m%d%H%M%S')), 'from the file name'
        except Exception: pass
    return os.path.getmtime(path), 'from the file date (may be off)'

def check_file(path, base, det, step=2.0, progress=None, stop_at=None, start_at=None):
    """Checks an ordinary real-time video: one frame every `step` seconds, timed from `base`. Returns seconds covered."""
    det.reset(); k = 0
    skip = max(0.0, (start_at or 0) - base)    # jump straight to the search window instead of decoding up to it
    base += skip
    for k, fr in enumerate(frames(path, 1.0 / step, seek=skip)):
        t = base + k * step
        if stop_at and t > stop_at: break
        det.feed(fr, t)
        if progress and k % 20 == 0: progress(t)
    return k * step

def tapo_recordings(cfg, frm, to):
    """Lists recordings on a Tapo camera's memory card between frm and to (unofficial pytapo library)."""
    from pytapo import Tapo
    tapo = Tapo(cfg['ip'], cfg.get('user', 'admin'), cfg['pw'], cfg.get('cloud_pw', ''))
    tc = tapo.getTimeCorrection() or 0            # camera clock -> real time
    items = []; day = datetime.date.fromtimestamp(frm)
    while day <= datetime.date.fromtimestamp(to):
        for rec in (tapo.getRecordings(day.strftime('%Y%m%d')) or []):
            for v in rec.values():
                s, e = int(v['startTime']), int(v['endTime'])
                if e + tc >= frm and s + tc <= to: items.append((s, e))
        day += datetime.timedelta(days=1)
    return tapo, tc, sorted(set(items))

def tapo_download(tapo, s, e, tc, folder):
    from pytapo.media_stream.downloader import Downloader
    name = f'tapo_{s}.mp4'
    status = asyncio.run(Downloader(tapo, s, e, tc, folder + '/', None, True, None, name).downloadFile())
    p = os.path.join(folder, name)
    return p if os.path.exists(p) else None

# ---------- TP-Link VIGI recorded-video playback (same method used to search our own cameras) ----------
def vigi_stream(ip, user, pw, start, wall, sink, scale='4/1', client_id=17):
    """Streams recorded video starting at `start` (epoch) for `wall` seconds into sink(bytes). Returns (bytes, last_error_code)."""
    import urllib.parse as up, websockets
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    md5 = lambda s: hashlib.md5(s.encode()).hexdigest()
    def auth(ch):
        p = hashlib.sha256(pw.encode()).hexdigest().upper(); nc = '00000001'; cn = os.urandom(8).hex()
        a1 = md5(f"{user}:{ch['realm']}:{p}"); a2 = md5('GET:/stream')
        return {'username': user, 'realm': ch['realm'], 'nonce': ch['nonce'], 'uri': '/stream', 'algorithm': ch['algorithm'],
                'response': md5(f"{a1}:{ch['nonce']}:{nc}:{cn}:{ch['qop']}:{a2}"), 'opaque': ch['opaque'], 'qop': ch['qop'], 'nc': nc, 'cnonce': cn}
    def mp(obj):
        j = json.dumps(obj, separators=(',', ':'))
        return '----client-stream-boundary--\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s\r\n' % (len(j), j)
    async def run():
        hdr = {'Content-Type': 'multipart/mixed;boundary=--client-stream-boundary--', 'X-SECURE-HASH-1': ''}
        sp = up.quote(json.dumps(hdr, separators=(',', ':')), safe="!'()*-._~")
        nb = 0; err = None
        async with websockets.connect(f'wss://{ip}:8443/stream', ssl=ctx, subprotocols=[sp], max_size=None, open_timeout=15) as ws:
            m = json.loads(await ws.recv())
            await ws.send(json.dumps({'type': 'request', 'seq': 0, 'params': {'method': 'do', 'authenticate': auth(m['params'])}}))
            await ws.send(mp({'type': 'request', 'seq': 0, 'params': {'method': 'get', 'playback': {'client_id': client_id, 'scale': scale, 'channels': [0],
                              'event_type': [1], 'event_filter': 0, 'start_time': str(int(start))}}}))
            t0 = time.time()
            while time.time() - t0 < wall:
                try: m = await asyncio.wait_for(ws.recv(), 10)
                except Exception: break
                if isinstance(m, bytes):
                    i = m.find(b'\r\n\r\n'); hd = m[:i]
                    if b'video/mp2t' in hd: sink(m[i + 4:]); nb += len(m) - i - 4
                    elif b'application/json' in hd:
                        mm = re.search(rb'"error_code":\s*(-?\d+)', m[i + 4:])
                        if mm: err = int(mm.group(1))
                else:
                    mm = re.search(r'"error_code":\s*(-?\d+)', m)
                    if mm: err = int(mm.group(1))
        return nb, err
    try:
        return asyncio.run(run())
    except Exception as ex:
        return 0, repr(ex)[:80]

def vigi_chunk(ip, user, pw, start, wall, out_ts, scale='4/1', client_id=17):
    with open(out_ts, 'wb') as fo:
        return vigi_stream(ip, user, pw, start, wall, fo.write, scale, client_id)


class Progress:
    def __init__(self, path, det, frm, to):
        import threading
        self.path, self.det, self.lock = path, det, threading.Lock()
        if not os.path.exists(path): write_json(path, {'status': 'running', 'from': frm, 'to': to, 'pos': frm, 'found': 0, 'activity': []})
        self.fmt = lambda t: datetime.datetime.fromtimestamp(t).strftime('%a %d %b %H:%M')
        det.on_found = lambda when, c, kind='cat': self.act(f'Possible {"cat" if kind == "cat" else "dog or cat"} at {when.strftime("%a %d %b %H:%M:%S")} ({int(c * 100)}%)')
        det.on_move = lambda when: self.act(f'Movement at {when.strftime("%a %d %b %H:%M:%S")} - check the photo')
    def P(self, **k):
        with self.lock:
            try: s = json.load(open(self.path))
            except Exception: s = {}
            s.update(k); s['found'] = self.det.found; s['moves'] = self.det.moves; write_json(self.path, s)
    def act(self, text):
        with self.lock:
            try: acts = json.load(open(self.path)).get('activity', [])
            except Exception: acts = []
        self.P(activity=(acts + [[time.time(), text]])[-40:])

def files_mode(a, det):
    """Checks video files someone downloaded (any camera brand)."""
    files = [f for f in a.files if os.path.exists(f)]
    starts = [file_start_time(f) for f in files]
    if a.starts:
        starts = [((float(x), 'set by you') if x not in ('', 'auto') else st) for x, st in zip(a.starts, starts)]
    frm = min(s for s, _ in starts) if starts else time.time()
    durs = [float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', f], capture_output=True, text=True).stdout.strip() or 0) for f in files]
    to = max((s + d for (s, _), d in zip(starts, durs)), default=frm)
    pr = Progress(os.path.join(a.out, f'search_{a.cam or "files"}.json'), det, frm, to)
    pr.P(started=time.time(), **{'from': frm, 'to': to})
    total = sum(durs) or 1; done = 0; started = time.time()
    for f, (s, how), d in zip(files, starts, durs):
        before = det.found; mv0 = det.moves
        pr.P(msg=f'Checking {os.path.basename(f)} (starts {pr.fmt(s)}, time {how})')
        def prog(t, done=done):
            el = time.time() - started; cov = max(1, done + (t - s))
            pr.P(pos=t, eta=max(0, (total - cov) * el / cov))
        check_file(f, s, det, step=1.0, progress=prog)
        done += d; new = det.found - before
        pr.act(f'Checked {os.path.basename(f)} ({pr.fmt(s)} - {pr.fmt(s + d)}, time {how}): ' + (plural(new, 'possible cat') if new else 'no cats') + (f', {plural(det.moves - mv0, "movement")}' if det.moves - mv0 else ''))
    pr.P(status='done', pos=to, msg='Finished', finished=time.time(), eta=0)
    pr.act(f'Finished: {plural(det.found, "possible cat photo")} found in {plural(len(files), "file")}')
    notify('Cat Watch', f'Video files checked: {plural(det.found, "possible cat photo")}')

def tapo_history(a, det, cfg):
    """Searches a Tapo camera's memory card: lists its recordings, downloads each one locally and checks it."""
    pr = Progress(os.path.join(a.out, f'search_{a.cam or "default"}.json'), det, a.frm, a.to)
    pr.P(started=time.time(), msg='Asking the Tapo camera for its list of recordings...')
    try:
        tapo, tc, items = tapo_recordings(cfg, a.frm, a.to)
    except Exception as ex:
        pr.act('Could not read the Tapo recordings: ' + repr(ex)[:150])
        pr.P(status='error', msg='Could not read the Tapo recordings. Check the Tapo account password and that the memory card has recordings.')
        return
    pr.act(f'The camera has {plural(len(items), "recording")} in this period')
    folder = os.path.join(a.out, '_tapo'); os.makedirs(folder, exist_ok=True)
    started = time.time()
    for n, (s, e) in enumerate(items, 1):
        rs = s + tc
        pr.P(msg=f'Downloading recording {n} of {len(items)} ({pr.fmt(rs)})', pos=max(a.frm, rs))
        try:
            path = tapo_download(tapo, s, e, tc, folder)
        except Exception as ex:
            pr.act(f'Could not download the recording at {pr.fmt(rs)}: ' + repr(ex)[:100]); gap_at(pr, rs); continue
        if not path: pr.act(f'Recording at {pr.fmt(rs)} is still being written, skipped'); gap_at(pr, rs); continue
        before = det.found
        pr.P(msg=f'Checking recording {n} of {len(items)} ({pr.fmt(rs)})')
        check_file(path, rs, det, step=1.0, stop_at=a.to, progress=lambda t: pr.P(pos=t), start_at=a.frm)
        try: os.remove(path)
        except Exception: pass
        el = time.time() - started; cov = max(1, (e + tc) - a.frm)
        pr.P(pos=min(e + tc, a.to), eta=max(0, (a.to - (e + tc)) * el / cov))
        pr.act(f'Checked {pr.fmt(rs)} - {pr.fmt(e + tc)}: ' + (plural(det.found - before, 'possible cat') if det.found - before else 'no cats'))
    pr.P(status='done', pos=a.to, msg='Finished', finished=time.time(), eta=0)
    pr.act(f'Finished: {plural(det.found, "possible cat photo")} found')
    notify('Cat Watch', f'Search finished: {plural(det.found, "possible cat photo")}')


# ---------- Hikvision cameras and recorders (ISAPI, Hikvision's published interface) ----------
def _hik_offset(cfg, auth):
    """The camera's UTC offset in seconds, from its own clock settings, or None if it does not say."""
    import requests
    try:
        r = requests.get(_hik_base(cfg) + '/ISAPI/System/time', auth=auth, timeout=10)
        m = re.search(r'<localTime>[^<]*?(Z|[+-]\d{2}:?\d{2})\s*</localTime>', r.text)
        if r.ok and m:
            z = m.group(1)
            if z == 'Z': return 0
            sign = -1 if z[0] == '-' else 1; hh, mm = int(z[1:3]), int(z[-2:])
            return sign * (hh * 3600 + mm * 60)
    except Exception:
        pass
    return None
def _hik_base(cfg): return (cfg.get('isapi') or f"http://{cfg['ip']}").rstrip('/')
def _hik_channel(cfg):
    m = re.search(r'Channels/(\d+?)0[12]\b', cfg.get('source') or '')
    return int(cfg.get('channel') or (m.group(1) if m else 1))
def _hik_time(ts, off):
    """Hikvision search times are the camera's own wall clock, written with a "Z" (most firmware reads them that way)."""
    if off is None: return datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%dT%H:%M:%SZ')
    return datetime.datetime.utcfromtimestamp(ts + off).strftime('%Y-%m-%dT%H:%M:%SZ')
def _hik_parse(v, off):
    import calendar
    v = v.strip()
    m = re.match(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?([+-]\d{2}:?\d{2})?', v)
    naive = time.strptime(m.group(1), '%Y-%m-%dT%H:%M:%S')
    if m.group(2):                                # an explicit offset is unambiguous
        z = m.group(2); sign = -1 if z[0] == '-' else 1
        return calendar.timegm(naive) - sign * (int(z[1:3]) * 3600 + int(z[-2:]) * 60)
    if off is None: return time.mktime(naive)     # camera clock assumed to match this computer
    return calendar.timegm(naive) - off           # camera wall clock -> real time
def hik_recordings(cfg, frm, to):
    """Lists recorded segments (start, end, playbackURI) for one channel between frm and to."""
    import requests, uuid
    from requests.auth import HTTPDigestAuth
    from xml.sax.saxutils import unescape
    auth = HTTPDigestAuth(cfg.get('user', 'admin'), cfg['pw'])
    off = _hik_offset(cfg, auth)
    track = f"{_hik_channel(cfg)}01"
    sid = str(uuid.uuid4()).upper()               # the same searchID for every page of one search
    items, seen, pos = [], set(), 0
    for _ in range(200):                          # at most 10,000 recordings; never loop forever
        body = ('<?xml version="1.0" encoding="utf-8"?><CMSearchDescription>'
                f'<searchID>{sid}</searchID><trackList><trackID>{track}</trackID></trackList>'
                f'<timeSpanList><timeSpan><startTime>{_hik_time(frm, off)}</startTime><endTime>{_hik_time(to, off)}</endTime></timeSpan></timeSpanList>'
                f'<maxResults>50</maxResults><searchResultPostion>{pos}</searchResultPostion>'
                '<metadataList><metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor></metadataList></CMSearchDescription>')
        r = requests.post(_hik_base(cfg) + '/ISAPI/ContentMgmt/search', data=body.encode(), auth=auth, timeout=30,
                          headers={'Content-Type': 'application/xml'})
        if r.status_code == 401: raise RuntimeError('the camera refused the username or password')
        r.raise_for_status()
        txt = r.text
        found = re.findall(r'<searchMatchItem>(.*?)</searchMatchItem>', txt, re.S)
        new = 0
        for it in found:
            s = re.search(r'<startTime>([^<]+)</startTime>', it); e = re.search(r'<endTime>([^<]+)</endTime>', it)
            u = re.search(r'<playbackURI>([^<]+)</playbackURI>', it)
            if s and e and u:
                key = (_hik_parse(s.group(1), off), _hik_parse(e.group(1), off), unescape(u.group(1)))
                if key not in seen: seen.add(key); items.append(key); new += 1
        pos += len(found)
        if 'MORE' not in txt or not new: break
    return sorted(items)
def hik_download(cfg, uri, path):
    import requests
    from requests.auth import HTTPDigestAuth
    from xml.sax.saxutils import escape
    body = f'<?xml version="1.0" encoding="utf-8"?><downloadRequest><playbackURI>{escape(uri)}</playbackURI></downloadRequest>'
    with requests.get(_hik_base(cfg) + '/ISAPI/ContentMgmt/download', data=body.encode(), stream=True, timeout=60,
                      auth=HTTPDigestAuth(cfg.get('user', 'admin'), cfg['pw']), headers={'Content-Type': 'application/xml'}) as r:
        r.raise_for_status()
        with open(path, 'wb') as f:
            for chunk in r.iter_content(1 << 20): f.write(chunk)
    return path if os.path.getsize(path) > 10000 else None

def gap_at(pr, t):
    """Remembers the first part of the search that could not be checked, so the next catch-up starts there."""
    try: g = json.load(open(pr.path)).get('gap')
    except Exception: g = None
    if g is None or t < g: pr.P(gap=t)

def segment_history(a, det, label, lister, downloader):
    """Search for cameras that hand over whole recordings: list them, download each one locally, check it, delete it."""
    pr = Progress(os.path.join(a.out, f'search_{a.cam or "default"}.json'), det, a.frm, a.to)
    pr.P(started=time.time(), msg=f'Asking the {label} camera for its list of recordings...')
    try:
        items = lister()
    except Exception as ex:
        pr.act(f'Could not read the {label} recordings: ' + repr(ex)[:150])
        pr.P(status='error', msg=f'Could not read the {label} recordings. Check the password and that the camera has recordings for these times.')
        return
    pr.act(f'The camera has {plural(len(items), "recording")} in this period')
    folder = os.path.join(a.out, '_download'); os.makedirs(folder, exist_ok=True)
    started = time.time()
    for n, (s, e, ref) in enumerate(items, 1):
        pr.P(msg=f'Downloading recording {n} of {len(items)} ({pr.fmt(s)})', pos=max(a.frm, s))
        path = os.path.join(folder, f'rec_{safe_name(a.cam or "cam", "cam")}_{os.getpid()}_{int(s)}.mp4')
        before = det.found
        try:
            try:
                got = downloader(ref, path)
            except Exception as ex:
                pr.act(f'Could not download the recording at {pr.fmt(s)}: ' + repr(ex)[:100]); gap_at(pr, s); continue
            if not got: pr.act(f'Recording at {pr.fmt(s)} could not be downloaded (still being written?), skipped'); gap_at(pr, s); continue
            pr.P(msg=f'Checking recording {n} of {len(items)} ({pr.fmt(s)})')
            check_file(got, s, det, step=1.0, stop_at=a.to, progress=lambda t: pr.P(pos=t), start_at=a.frm)
        finally:
            try: os.remove(path)
            except Exception: pass
        el = time.time() - started; cov = max(1, min(e, a.to) - a.frm)
        pr.P(pos=min(e, a.to), eta=max(0, (a.to - min(e, a.to)) * el / cov))
        pr.act(f'Checked {pr.fmt(max(s, a.frm))} - {pr.fmt(min(e, a.to))}: ' + (plural(det.found - before, 'possible cat') if det.found - before else 'no cats'))
    pr.P(status='done', pos=a.to, msg='Finished', finished=time.time(), eta=0)
    pr.act(f'Finished: {plural(det.found, "possible cat photo")} found')
    notify('Cat Watch', f'Search finished: {plural(det.found, "possible cat photo")}')

def history(a, det, cfg):
    """Searches past recordings. Video from the camera is checked frame by frame while it is still arriving."""
    gap = [None]; busy_seen = False
    import threading
    prog = os.path.join(a.out, f'search_{a.cam or "default"}.json')
    fmt = lambda t: datetime.datetime.fromtimestamp(t).strftime('%a %d %b %H:%M')
    lock = threading.Lock()
    def P(**k):
        with lock:
            s = {'status': 'running', 'from': a.frm, 'to': a.to, 'pos': a.frm, 'found': det.found, 'msg': ''}
            try: s.update(json.load(open(prog)))
            except Exception: pass
            s.update(k); s['found'] = det.found; s['moves'] = det.moves; write_json(prog, s)
    def act(text):
        with lock:
            try: acts = json.load(open(prog)).get('activity', [])
            except Exception: acts = []
        P(activity=(acts + [[time.time(), text]])[-40:])
    det.on_found = lambda when, c, kind='cat': act(f'Possible {"cat" if kind == "cat" else "dog or cat"} at {when.strftime("%a %d %b %H:%M:%S")} ({int(c * 100)}%)')
    det.on_move = lambda when: act(f'Movement at {when.strftime("%a %d %b %H:%M:%S")} - check the photo')
    if not os.path.exists(prog): write_json(prog, {'status': 'running', 'from': a.frm, 'to': a.to, 'pos': a.frm, 'found': 0, 'activity': []})
    scale = 4; WALL = 60; started = time.time(); pos = a.frm; fails = 0
    P(started=started)
    W, H = 1280, 720; n = W * H * 3
    while pos < a.to:
        base = a.test_start if a.test_chunk else pos
        P(msg=f'Receiving recordings from {fmt(base)} and checking them as they arrive')
        ff = subprocess.Popen(['ffmpeg', '-v', 'error', '-f', 'mpegts', '-i', 'pipe:0', '-vf', f'fps=2,scale={W}:{H}', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        CHILDREN.append(ff)
        res = {}
        def feed_input():
            try:
                if a.test_chunk:                        # offline test: send a local recording through the same path
                    with open(a.test_chunk, 'rb') as f:
                        tot = 0
                        while True:
                            b = f.read(1 << 20)
                            if not b: break
                            ff.stdin.write(b); tot += len(b)
                    res['r'] = (tot, None)
                else:
                    res['r'] = vigi_stream(cfg['ip'], cfg.get('user', 'admin'), cfg['pw'], base, WALL, ff.stdin.write, f'{scale}/1')
            except Exception as ex:
                res['r'] = (0, repr(ex)[:80])
            finally:
                try: ff.stdin.close()
                except Exception: pass
        th = threading.Thread(target=feed_input, daemon=True); th.start()
        det.reset(); before = det.found; mv0 = det.moves; k = 0; t = base; hit_end = False
        if fails == 0: busy_seen = False
        while True:
            b = ff.stdout.read(n)
            if len(b) < n: break
            t = base + (k / 2.0) * scale
            if t > a.to: hit_end = True; break
            det.feed(np.frombuffer(b, np.uint8).reshape(H, W, 3), t)
            if k % 20 == 0:
                el = time.time() - started; cov = max(1, t - a.frm)
                P(pos=t, eta=max(0, (a.to - t) * el / cov), msg=f'Checking {fmt(t)} as it arrives from the camera')
            k += 1
        ff.kill(); th.join(5)
        nb, err = res.get('r', (0, 'no result'))
        real = (k / 2.0) * scale
        if not hit_end and (nb < 300000 or k < 10):
            fails += 1; log(a.out, 'no video for', datetime.datetime.fromtimestamp(base).strftime('%d/%m %H:%M'), 'error', err)
            if err not in (0, '0', None, ''): busy_seen = True    # busy, not answering, or cut off: come back to it later (0 = nothing recorded)
            midnight = datetime.datetime.combine(datetime.date.fromtimestamp(base) + datetime.timedelta(days=1), datetime.time()).timestamp()
            if 0 < midnight - base <= 60 and not busy_seen:      # end of the day's recordings: go straight on to the next day
                act(f'Reached the end of {datetime.datetime.fromtimestamp(base).strftime("%a %d %b")} recordings, carrying on from midnight')
                pos = midnight; fails = 0; continue
            act(f'No video received for {fmt(base)} (camera busy or nothing recorded), trying again')
            if fails >= 4:
                act(f'Skipped 5 minutes at {fmt(base)}: the camera gave no recording')
                recent = time.time() - base < 1200       # the camera may not have finished saving the last few minutes yet
                if (busy_seen or recent) and not gap[0]: gap[0] = base; P(gap=base)    # checked again next time
                midnight = datetime.datetime.combine(datetime.date.fromtimestamp(base) + datetime.timedelta(days=1), datetime.time()).timestamp()
                # the camera keeps each day separately: just before midnight, carry on at midnight, not 5 minutes later
                pos = midnight if 0 < midnight - base <= 300 else pos + 300
                fails = 0; busy_seen = False
            if a.test_chunk: break
            time.sleep(15); continue
        fails = 0; end = min(base + real, a.to); new = det.found - before
        el = time.time() - started; cov = max(1, end - a.frm)
        P(pos=end, eta=max(0, (a.to - end) * el / cov))
        act(f'Checked {fmt(base)} - {fmt(end)}: ' + (plural(new, 'possible cat') if new else 'no cats') + (f', {plural(det.moves - mv0, "movement")}' if det.moves - mv0 else ''))
        if a.test_chunk or hit_end: break
        pos = (base + real - 20) if real >= 90 else pos + max(real - 5, 20)
    P(status='done', pos=a.to, msg='Finished', finished=time.time(), eta=0)
    act(f'Finished: {plural(det.found, "possible cat photo")} found')
    log(a.out, 'search finished, possible cats:', det.found)
    notify('Cat Watch', f'Search finished: {plural(det.found, "possible cat photo")}')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', default=os.environ.get('CATWATCH_SOURCE', ''), help='rtsp://user:pass@ip:554/stream1 OR a video file (live mode)')
    ap.add_argument('--name', default='camera')
    ap.add_argument('--out', default=os.path.expanduser('~/cat-watch-snapshots'))
    ap.add_argument('--model', default='auto')
    ap.add_argument('--fps', type=float, default=2.0)
    ap.add_argument('--conf', type=float, default=0.30)
    ap.add_argument('--cooldown', type=int, default=None, help='seconds between saved photos (default 600 live, 120 when searching)')
    ap.add_argument('--always-check', type=int, default=20)
    ap.add_argument('--from', dest='frm', type=float, help='search past recordings starting at this time (epoch seconds)')
    ap.add_argument('--to', type=float, help='end of the search (epoch seconds, default now)')
    ap.add_argument('--cam', help='camera id from the setup (default: the first camera)')
    ap.add_argument('--files', nargs='+', help='check these downloaded video files (any camera brand)')
    ap.add_argument('--starts', nargs='+', help='recording start time of each file (epoch seconds, or auto)')
    ap.add_argument('--test-chunk', help=argparse.SUPPRESS); ap.add_argument('--test-start', type=float, help=argparse.SUPPRESS)
    a = ap.parse_args()
    if not a.test_chunk and os.environ.get('CATWATCH_TEST_CHUNK'):
        a.test_chunk = os.environ['CATWATCH_TEST_CHUNK']; a.test_start = float(os.environ['CATWATCH_TEST_START'])
    os.makedirs(a.out, exist_ok=True)
    cfgp = os.path.join(os.environ.get('CATWATCH_HOME', os.path.expanduser('~/.catwatch')), 'config.json')
    data = json.load(open(cfgp)) if os.path.exists(cfgp) else {}
    if a.files:
        cfg = {}                                  # video files do not belong to a camera
    elif 'cams' in data:
        cams = data['cams']; cfg = next((c for c in cams if c.get('id') == a.cam), cams[0] if cams else {})
        a.cam = cfg.get('id', a.cam)
    else:
        cfg = data
    if not a.source: a.source = cfg.get('source', '')
    if a.name == 'camera': a.name = cfg.get('name', 'camera')
    from ultralytics import YOLO
    mname = pick_model(a.model); dev = device_name(); model = YOLO(mname)
    if a.files:
        det = Detector(model, dev, a.out, safe_name(a.name if a.name != 'camera' else 'Video files', 'Video-files'), a.conf, a.cooldown or 120, a.always_check, live=False)
        det.mode_tag = 'FILE'
        log(a.out, 'checking video files', len(a.files)); files_mode(a, det); return
    if a.frm:
        a.to = a.to or time.time()
        if not a.test_chunk and not (cfg.get('ip') and cfg.get('pw')):
            sys.exit('Set up the camera first (python setup.py or the Cat Watch page).')
        det = Detector(model, dev, a.out, safe_name(a.name, a.cam or 'camera'), a.conf, a.cooldown or 120, a.always_check, live=False)
        log(a.out, 'searching past recordings', a.name, datetime.datetime.fromtimestamp(a.frm), '->', datetime.datetime.fromtimestamp(a.to), 'model', mname, dev)
        b = cfg.get('brand') or ''
        if 'Tapo' in b: tapo_history(a, det, cfg)
        elif 'Hikvision' in b:
            segment_history(a, det, 'Hikvision', lambda: hik_recordings(cfg, a.frm, a.to), lambda uri, p: hik_download(cfg, uri, p))
        else: history(a, det, cfg)
        return
    if not a.source: sys.exit('No camera set up yet. Run: python setup.py   (or pass --source)')
    det = Detector(model, dev, a.out, safe_name(a.name, a.cam or 'camera'), a.conf, a.cooldown or 600, a.always_check, live=True)
    log(a.out, 'watching live', a.name, 'model', mname, dev)
    is_file = os.path.exists(a.source); t0 = time.time()
    while True:
        for k, fr in enumerate(frames(a.source, a.fps)):
            det.feed(fr, (t0 + k / a.fps) if is_file else time.time())
        if is_file: break
        log(a.out, 'stream ended or unavailable, retrying in 20 s'); time.sleep(20)
    log(a.out, 'finished')

if __name__ == '__main__':
    main()
