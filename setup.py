#!/usr/bin/env python3
"""Cat Watch setup: finds cameras on YOUR OWN network, then asks YOU for the camera password.
The password is typed on this computer, saved only in a private file on this computer, and never sent anywhere."""
import argparse, concurrent.futures as cf, getpass, json, os, socket, ssl, subprocess, sys, re, urllib.request, urllib.error

CONF_DIR = os.environ.get('CATWATCH_HOME', os.path.expanduser('~/.catwatch'))
CONF = os.path.join(CONF_DIR, 'config.json')

def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1)); return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        s.close()

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
    tmp = path + '.tmp'
    with open(tmp, 'w') as f: json.dump(obj, f)
    os.replace(tmp, path)

def try_rtsp(ip, user, pw):
    """One login attempt per call: a wrong password stops at the first address, so the camera counts one failure."""
    from urllib.parse import quote
    for path in ('stream1', 'stream2'):
        url = f'rtsp://{quote(user, safe="")}:{quote(pw, safe="")}@{ip}:554/{path}'
        try:
            r = subprocess.run(['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp', '-show_entries', 'stream=codec_name',
                                '-of', 'csv=p=0', url], capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            return None
        if r.returncode == 0 and r.stdout.strip():
            return url
        if '401' in r.stderr or 'Unauthorized' in r.stderr:
            return None                      # wrong password: do not spend a second login on the other address
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hosts', help='comma-separated IPs to check instead of scanning your whole network')
    ap.add_argument('--find-only', action='store_true', help='just list cameras, do not ask for a password')
    a = ap.parse_args()
    if a.hosts:
        hosts = [h.strip() for h in a.hosts.split(',')]
        print('Checking only:', ', '.join(hosts))
    else:
        me = local_ip(); base = '.'.join(me.split('.')[:3])
        print(f'This computer is {me}. Looking for cameras on {base}.0/24 (your own network only)...')
        hosts = [f'{base}.{i}' for i in range(1, 255) if f'{base}.{i}' != me]
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
        url = try_rtsp(ip, user, pw)
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
