#!/usr/bin/env python3
"""Cat Watch setup: finds cameras on YOUR OWN network, then asks YOU for the camera password.
The password is typed on this computer, saved only in a private file on this computer, and never sent anywhere."""
import argparse, concurrent.futures as cf, getpass, ipaddress, json, os, socket, ssl, subprocess, sys, re, time, urllib.request, urllib.error

CONF_DIR = os.environ.get('CATWATCH_HOME', os.path.expanduser('~/.catwatch'))
CONF = os.path.join(CONF_DIR, 'config.json')

_IF_HDR_RE = re.compile(r'^([a-zA-Z0-9_.-]+):\s+flags=\d+<([^>]*)>', re.M)
_SKIP_IF_RE = re.compile(r'^(lo|utun|tun|tap|wg|ppp|gif|stf|awdl|llw|anpi|bridge|docker|veth|br-|virbr)')
_INET_RE = re.compile(r'^\s+inet\s+(?:addr:)?(\d+\.\d+\.\d+\.\d+)([^\n]*)', re.M)
_PTP_MASK_RE = re.compile(r'(?:netmask\s+|Mask:)(?:0xffffffff|255\.255\.255\.255)\b')

def _is_lan_ip(ip):
    """Return True if ip is a private, routable IPv4 LAN address."""
    try:
        addr = ipaddress.ip_address(ip)
        return addr.version == 4 and addr.is_private and not (addr.is_loopback or addr.is_link_local or addr.is_unspecified or addr.is_multicast)
    except ValueError:
        return False

def _parse_ifconfig(text):
    """Parse ifconfig output into (lan_ips, skipped_ptp_ips)."""
    lan_ips, skip_ips = [], set()
    matches = list(_IF_HDR_RE.finditer(text or ''))
    for idx, m in enumerate(matches):
        name, flags_str = m.group(1), m.group(2)
        flags = set(flags_str.split(','))
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[m.end():end]
        if_skipped = bool(flags & {'LOOPBACK', 'POINTOPOINT'}) or bool(_SKIP_IF_RE.match(name))
        for im in _INET_RE.finditer(body):
            ip, rest = im.group(1), im.group(2)
            has_bcast = 'broadcast' in rest or 'Bcast:' in rest
            is_ptp_addr = '-->' in rest or 'P-t-P:' in rest or bool(_PTP_MASK_RE.search(rest))
            if if_skipped or (is_ptp_addr and not has_bcast) or not _is_lan_ip(ip):
                skip_ips.add(ip)
            elif ip not in lan_ips:
                lan_ips.append(ip)
    return lan_ips, skip_ips

def _parse_ifconfig_ips(text):
    """Extract private broadcast LAN IPv4 addresses from ifconfig output."""
    return _parse_ifconfig(text)[0]

def local_ips():
    """Return private LAN IPv4 addresses, preferring physical broadcast interfaces over VPN tunnels."""
    ips, skip_ips = [], set()
    try:
        r = subprocess.run(['ifconfig'], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout:
            ips, skip_ips = _parse_ifconfig(r.stdout)
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if _is_lan_ip(ip) and ip not in skip_ips and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    if not ips:
        for dst in ('192.168.0.1', '192.168.1.1', '10.255.255.255'):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((dst, 1))
                ip = s.getsockname()[0]
                if _is_lan_ip(ip) and ip not in skip_ips and ip not in ips:
                    ips.append(ip)
            except Exception:
                pass
            finally:
                s.close()
    return ips

def local_ip():
    """Return the primary local LAN IPv4 address, or 127.0.0.1 if unavailable."""
    ips = local_ips()
    return ips[0] if ips else '127.0.0.1'

def lan_hosts():
    """Return all /24 host IPs for the active local LAN interfaces, excluding own IPs."""
    ips = [ip for ip in (local_ips() or [local_ip()]) if ip and ip != '127.0.0.1']
    own, hosts, seen = set(ips), [], set()
    for ip in ips:
        base = '.'.join(ip.split('.')[:3])
        for i in range(1, 255):
            h = f'{base}.{i}'
            if h not in own and h not in seen:
                seen.add(h)
                hosts.append(h)
    return hosts

def port_open(ip, port, t=0.5):
    try:
        with socket.create_connection((ip, port), timeout=t): return True
    except Exception:
        return False

def onvif_discovery(wait=3.0):
    """Standard ONVIF 'who is a camera here?' broadcast on the local network (many cameras answer it)."""
    msg = ('<?xml version="1.0" encoding="UTF-8"?><e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
           'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
           'xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><e:Header><w:MessageID>uuid:84ede3de-7dec-11d0-c360-f01234567890</w:MessageID>'
           '<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To><w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
           '</e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>')
    found = set()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.5)
    try:
        ip = local_ip()
        if ip != '127.0.0.1':
            try: s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            except Exception: pass
        s.sendto(msg.encode(), ('239.255.255.250', 3702))
        import time; end = time.time() + wait
        while time.time() < end:
            try:
                data, addr = s.recvfrom(65535); found.add(addr[0])
            except socket.timeout:
                pass
            except Exception:
                break
    except Exception:
        pass
    finally:
        s.close()
    return found

def identify(ip):
    """Returns (brand_guess, details) or None if it does not look like a camera."""
    rtsp = port_open(ip, 554); https = port_open(ip, 443); alt = port_open(ip, 8443)
    brand = None
    web_page = False
    if https:
        try:                                     # VIGI cameras serve a web page; Tapo cameras do not
            ctx0 = ssl.create_default_context(); ctx0.check_hostname = False; ctx0.verify_mode = ssl.CERT_NONE
            web_page = b'<html' in urllib.request.urlopen(f'https://{ip}/', context=ctx0, timeout=8).read(2000).lower()
        except Exception:
            pass
    if https:
        try:
            ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(f'https://{ip}/', data=json.dumps({"method": "do", "login": {"encrypt_type": "2"}}).encode(),
                                         headers={'Content-Type': 'application/json'})
            try:
                body = urllib.request.urlopen(req, context=ctx, timeout=8).read().decode('utf8', 'ignore')
            except urllib.error.HTTPError as he:      # VIGI answers this first step with an HTTP error code plus JSON
                body = he.read().decode('utf8', 'ignore')
            if '"encrypt_type"' in body and 'nonce' in body:
                brand = 'TP-Link VIGI' if web_page else 'TP-Link Tapo'
        except Exception:
            pass
    if not brand and port_open(ip, 80):
        try:                                     # Hikvision answers its ISAPI address with a login challenge
            urllib.request.urlopen(f'http://{ip}/ISAPI/System/deviceInfo', timeout=5)
        except urllib.error.HTTPError as he:
            try: page = he.read(4000).decode('utf8', 'ignore')
            except Exception: page = ''
            srv = (he.headers.get('Server') or '') + ' ' + (he.headers.get('WWW-Authenticate') or '') + ' ' + page
            if he.code == 401 and (re.search(r'App-webs|DNVRS-Webs|DVRDVS-Webs|hikvision\.com|Hikvision', srv, re.I)
                                   or re.search(r'realm="(IP Camera\(|DS-|NVR\(|DVR\()', srv)
                                   or port_open(ip, 8000)):          # 8000 is Hikvision's own device port
                brand = 'Hikvision'
        except Exception:
            pass
    if not brand and port_open(ip, 80):
        try:                                     # Hikvision's web page sends the browser to /doc/...
            home = urllib.request.urlopen(f'http://{ip}/', timeout=5).read(3000).decode('utf8', 'ignore')
            if re.search(r'/doc/page/login\.asp|/doc/index\.html', home): brand = 'Hikvision'
        except Exception:
            pass
    if brand or rtsp:
        return (brand or 'camera (brand unknown)', {'rtsp_port_open': rtsp, 'https': https, 'stream_port_8443': alt})
    return None

def scan(hosts):
    out = {}
    with cf.ThreadPoolExecutor(64) as ex:
        for ip, res in zip(hosts, ex.map(identify, hosts)):
            if res: out[ip] = res
    return out

def write_json(path, obj):
    """Writes a whole file at once. Windows refuses to replace a file another program is reading, so retry briefly."""
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf8') as f: json.dump(obj, f)
    for i in range(20):
        try: os.replace(tmp, path); return
        except PermissionError:
            if i == 19: raise
            time.sleep(0.05)

def hik_device(base, user, pw):
    """Asks a Hikvision camera or recorder what it is and which cameras (channels) it has.
    Returns {'type', 'model', 'name', 'channels': [{'ch', 'name', 'ip', 'online'}]}. Raises PermissionError on a wrong password."""
    import requests
    from requests.auth import HTTPDigestAuth
    auth = HTTPDigestAuth(user, pw)
    def get(path):
        r = requests.get(base.rstrip('/') + path, auth=auth, timeout=10)
        if r.status_code == 401: raise PermissionError('wrong username or password')
        return r.text if r.ok else ''
    tag = lambda x, t: (re.search(rf'<{t}>([^<]*)</{t}>', x) or [None, ''])[1].strip()
    info = get('/ISAPI/System/deviceInfo')
    dev = {'type': tag(info, 'deviceType') or 'camera', 'model': tag(info, 'model'), 'name': tag(info, 'deviceName'), 'channels': []}
    online = {}
    for st in re.findall(r'<InputProxyChannelStatus\b.*?</InputProxyChannelStatus>', get('/ISAPI/ContentMgmt/InputProxy/channels/status'), re.S):
        online[tag(st, 'id')] = tag(st, 'online').lower() == 'true'
    for c in re.findall(r'<InputProxyChannel\b.*?</InputProxyChannel>', get('/ISAPI/ContentMgmt/InputProxy/channels'), re.S):
        cid = tag(c, 'id')                                   # cameras plugged into a recorder
        if cid.isdigit(): dev['channels'].append({'ch': int(cid), 'name': tag(c, 'name') or f'Camera {cid}', 'ip': tag(c, 'ipAddress'), 'online': online.get(cid)})
    if not dev['channels']:
        for c in re.findall(r'<VideoInputChannel\b.*?</VideoInputChannel>', get('/ISAPI/System/Video/inputs/channels'), re.S):
            cid = tag(c, 'id')                               # a camera itself, or a recorder's own wired inputs
            if cid.isdigit() and tag(c, 'videoInputEnabled').lower() != 'false':
                dev['channels'].append({'ch': int(cid), 'name': tag(c, 'name') or f'Camera {cid}', 'ip': '', 'online': None})
    if not dev['channels']: dev['channels'] = [{'ch': 1, 'name': dev['name'] or 'Camera 1', 'ip': '', 'online': None}]
    dev['channels'].sort(key=lambda c: c['ch'])
    return dev

def try_rtsp(ip, user, pw, brand='', channel=1):
    return probe_rtsp(ip, user, pw, brand, channel)[0]

def probe_rtsp(ip, user, pw, brand='', channel=1):
    """Returns (url, why). why is '' on success, 'password' if the camera refused the login, 'timeout' if it never
    answered, or 'other'. A refused login stops at the first address, so the camera counts only one failure."""
    from urllib.parse import quote
    why = 'other'
    ch = max(1, int(channel or 1))
    paths = (f'Streaming/Channels/{ch}01', f'Streaming/Channels/{ch}02') if 'Hikvision' in (brand or '') else \
            ('stream1', 'stream2') if 'TP-Link' in (brand or '') else ('stream1', 'Streaming/Channels/101', 'stream2')
    for path in paths:
        url = f'rtsp://{quote(user, safe="")}:{quote(pw, safe="")}@{ip}:554/{path}'
        try:
            r = subprocess.run(['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp', '-show_entries', 'stream=codec_name',
                                '-of', 'csv=p=0', url], capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            why = 'timeout'; continue        # slow to answer is not a wrong password: try the next address
        if r.returncode == 0 and r.stdout.strip():
            return url, ''
        if '401' in r.stderr or 'Unauthorized' in r.stderr:
            return None, 'password'          # wrong password: do not spend a second login on the other address
    return None, why                         # 'timeout' if any address never answered, so it was not a real login test

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hosts', help='comma-separated IPs to check instead of scanning your whole network')
    ap.add_argument('--find-only', action='store_true', help='just list cameras, do not ask for a password')
    a = ap.parse_args()
    if a.hosts:
        hosts = [h.strip() for h in a.hosts.split(',')]
        print('Checking only:', ', '.join(hosts))
    else:
        ips = local_ips() or [local_ip()]
        subnets = ', '.join('.'.join(ip.split('.')[:3]) + '.0/24' for ip in ips)
        print(f'This computer is {", ".join(ips)}. Looking for cameras on {subnets} (your own network only)...')
        hosts = lan_hosts()
        extra = onvif_discovery()
        if extra: print('Cameras that answered the ONVIF broadcast:', ', '.join(sorted(extra)))
        hosts = sorted(set(hosts) | extra, key=lambda x: tuple(int(p) for p in x.split('.')))
    cams = scan(hosts)
    if not cams:
        print('No cameras found. Check that you are on the same Wi-Fi/network as the camera, or run with --hosts IP.'); return
    ips = sorted(cams, key=lambda x: tuple(int(p) for p in x.split('.')))
    print('\nFound:')
    for n, ip in enumerate(ips, 1): print(f'  {n}. {ip:15s} {cams[ip][0]}  {cams[ip][1]}')
    if a.find_only: return
    pick = input('\nWhich one do you want to watch? Number: ').strip()
    ip = ips[int(pick) - 1]
    name = input('A name for it (e.g. frontdoor): ').strip() or 'camera'
    user = input('Camera username (often admin): ').strip() or 'admin'
    print('\nThe password is typed here, stays on this computer and is never sent to anyone.')
    for attempt in range(1, 4):
        pw = getpass.getpass('Camera password (hidden): ')
        print('Testing the stream address...')
        url = try_rtsp(ip, user, pw, cams[ip][0])
        if url: break
        print('Could not open the video stream. Wrong password, or the camera has no RTSP account yet.')
        if attempt == 3: print('Stopping after 3 tries so the camera does not lock you out. Check the camera settings (RTSP/ONVIF account) and run setup again.'); return
        print('(Repeated wrong passwords can lock the camera for a while, so try carefully.)')
    os.makedirs(CONF_DIR, exist_ok=True)
    try: d = json.load(open(CONF))
    except Exception: d = {}
    if 'cams' not in d: d = {'cams': [dict(d, id='cam1')] if d.get('source') else [], **({'missing_since': d['missing_since']} if 'missing_since' in d else {})}
    ex = next((c for c in d['cams'] if c.get('ip') == ip), None)
    if ex is None:
        n = 1
        while any(c.get('id') == f'cam{n}' for c in d['cams']): n += 1
        ex = {'id': f'cam{n}'}; d['cams'].append(ex)
    ex.update({'name': name, 'ip': ip, 'user': user, 'pw': pw, 'brand': cams[ip][0], 'source': url})
    write_json(CONF, d); os.chmod(CONF, 0o600)
    print(f'\nSaved to {CONF} (private to your user account).')
    print('Start watching with:  python agent.py')

if __name__ == '__main__':
    main()
