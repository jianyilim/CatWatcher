"""Unit tests for LAN discovery over VPN in setup.py and ui.py."""
import atexit
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest import mock

_TMP_HOME = tempfile.mkdtemp(prefix='catwatch_test_home_')
_TMP_OUT = tempfile.mkdtemp(prefix='catwatch_test_out_')
atexit.register(shutil.rmtree, _TMP_HOME, ignore_errors=True)
atexit.register(shutil.rmtree, _TMP_OUT, ignore_errors=True)
os.environ.setdefault('CATWATCH_HOME', _TMP_HOME)
os.environ.setdefault('CATWATCH_OUT', _TMP_OUT)

import setup  # noqa: E402
import ui  # noqa: E402


SAMPLE_MACOS_IFCONFIG = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.0.106 netmask 0xffffff00 broadcast 192.168.0.255
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1420
\tinet 10.5.5.2 --> 10.5.5.2 netmask 0xffffffff
awdl0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 169.254.12.34 netmask 0xffff0000 broadcast 169.254.255.255
"""


class FakeSock:
    """Minimal UDP socket double returning a configured local IP per destination."""

    def __init__(self, mapping):
        self._mapping = mapping
        self._ip = '127.0.0.1'

    def connect(self, addr):
        host, _ = addr
        if host not in self._mapping:
            raise OSError('unreachable')
        self._ip = self._mapping[host]

    def getsockname(self):
        return (self._ip, 12345)

    def close(self):
        pass


class TestLocalIpDiscovery(unittest.TestCase):
    """Tests for VPN-safe local network IP discovery and subnet scanning."""

    def test_parse_ifconfig_ips_ignores_loopback_and_vpn_utun(self):
        """Extracts only physical broadcast LAN IPs and skips loopback, link-local, and VPN tunnels."""
        self.assertEqual(setup._parse_ifconfig_ips(SAMPLE_MACOS_IFCONFIG), ['192.168.0.106'])

    def test_local_ip_prefers_wifi_lan_over_utun_vpn(self):
        """Returns 192.168.0.106 even when utun4 (10.5.5.2) handles default/10.x routes."""
        proc = subprocess.CompletedProcess(['ifconfig'], 0, stdout=SAMPLE_MACOS_IFCONFIG, stderr='')
        getaddr_rows = [
            (socket.AF_INET, socket.SOCK_DGRAM, 17, '', ('127.0.0.1', 0)),
            (socket.AF_INET, socket.SOCK_DGRAM, 17, '', ('10.5.5.2', 0)),
            (socket.AF_INET, socket.SOCK_DGRAM, 17, '', ('192.168.0.106', 0)),
        ]
        with mock.patch('setup.subprocess.run', return_value=proc), \
             mock.patch('setup.socket.getaddrinfo', return_value=getaddr_rows):
            self.assertEqual(setup.local_ips(), ['192.168.0.106'])
            self.assertEqual(setup.local_ip(), '192.168.0.106')

    def test_local_ips_falls_back_when_ifconfig_unavailable(self):
        """Falls back to getaddrinfo and UDP probes when ifconfig is not installed."""
        with mock.patch('setup.subprocess.run', side_effect=FileNotFoundError('ifconfig')), \
             mock.patch('setup.socket.getaddrinfo', return_value=[]), \
             mock.patch('setup.socket.socket', side_effect=lambda *a, **kw: FakeSock({'192.168.0.1': '192.168.0.42'})):
            self.assertEqual(setup.local_ips(), ['192.168.0.42'])
            self.assertEqual(setup.local_ip(), '192.168.0.42')

    def test_ui_scan_uses_local_ips_subnet(self):
        """ui.scan(None) scans the 192.168.0.0/24 subnet derived from local_ips()."""
        scanned_hosts = []

        def fake_scan(hosts):
            scanned_hosts.extend(hosts)
            return {
                '192.168.0.91': ('TP-Link VIGI', {'rtsp_port_open': True, 'https': True, 'stream_port_8443': True}),
                '192.168.0.220': ('TP-Link VIGI', {'rtsp_port_open': True, 'https': True, 'stream_port_8443': True}),
            }

        with mock.patch('setup.local_ips', return_value=['192.168.0.106']), \
             mock.patch('setup.onvif_discovery', return_value=set()), \
             mock.patch('setup.scan', side_effect=fake_scan), \
             mock.patch('ui.load', return_value={'cams': []}):
            result = ui.scan(None)

        self.assertEqual(len(scanned_hosts), 253)
        self.assertIn('192.168.0.91', scanned_hosts)
        self.assertIn('192.168.0.220', scanned_hosts)
        self.assertNotIn('192.168.0.106', scanned_hosts)
        self.assertFalse(any(ip.startswith('10.5.5.') for ip in scanned_hosts))
        self.assertEqual([c['ip'] for c in result], ['192.168.0.91', '192.168.0.220'])
        self.assertEqual(result[0]['brand'], 'TP-Link VIGI')


if __name__ == '__main__':
    unittest.main()
