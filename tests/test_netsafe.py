import http.server
import threading
import os
import unittest
import urllib.request
from unittest import mock

import _env  # noqa: F401
import netsafe


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/redirect-private':
            self.send_response(302)
            self.send_header('Location', 'http://169.254.169.254/latest/meta-data/')
            self.end_headers()
            return
        if self.path == '/redirect-loop':
            self.send_response(302)
            self.send_header('Location', '/redirect-loop')
            self.end_headers()
            return
        body = b'x' * 5000
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class NetsafeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_blocks_private_and_special_addresses(self):
        for ip in ['127.0.0.1', '10.0.0.5', '192.168.1.10', '172.16.3.4', '169.254.169.254',
                   '100.64.1.1', '0.0.0.0', '::1', 'fe80::1', 'fc00::1', '::ffff:127.0.0.1',
                   '224.0.0.1', '255.255.255.255']:
            self.assertFalse(netsafe.is_ip_allowed(ip), ip)
        for ip in ['93.184.216.34', '1.1.1.1', '2606:4700:4700::1111']:
            self.assertTrue(netsafe.is_ip_allowed(ip), ip)

    def test_rejects_bad_schemes_and_credentials(self):
        for url in ['ftp://example.com/x', 'file:///etc/passwd', 'javascript:alert(1)',
                    'http://user:pw@example.com/', 'http:///nohost', '']:
            with self.assertRaises(netsafe.BlockedURL, msg=url):
                netsafe.check_url(url)

    def test_loopback_url_is_blocked_by_default(self):
        with self.assertRaises(netsafe.BlockedURL):
            netsafe.fetch(f'http://127.0.0.1:{self.port}/ok')
        with self.assertRaises(netsafe.BlockedURL):
            netsafe.fetch(f'http://localhost:{self.port}/ok')

    def _allow_loopback_only(self):
        orig = netsafe.is_ip_allowed
        return mock.patch.object(netsafe, 'is_ip_allowed',
                                 side_effect=lambda ip: ip == '127.0.0.1' or orig(ip))

    def test_redirect_to_private_is_blocked(self):
        with self._allow_loopback_only(), mock.patch.object(netsafe, '_proxy_configured', return_value=False):
            with self.assertRaises(netsafe.BlockedURL):
                netsafe.fetch(f'http://127.0.0.1:{self.port}/redirect-private')

    def test_byte_limit_and_redirect_cap(self):
        with self._allow_loopback_only(), mock.patch.object(netsafe, '_proxy_configured', return_value=False):
            r = netsafe.fetch(f'http://127.0.0.1:{self.port}/ok', limit=1000)
            self.assertEqual(len(r.body), 1000)
            self.assertTrue(r.truncated)
            with self.assertRaises(Exception):
                netsafe.fetch(f'http://127.0.0.1:{self.port}/redirect-loop')

    def test_peer_check_catches_rebinding(self):
        # DNS check passes (pretend public), but the socket connects to 127.0.0.1.
        with mock.patch.object(netsafe, '_resolve', return_value=['93.184.216.34']), \
             mock.patch.object(netsafe, '_proxy_configured', return_value=False):
            with self.assertRaises(netsafe.BlockedURL):
                netsafe.fetch(f'http://127.0.0.1:{self.port}/ok')

    def test_peer_check_applies_to_no_proxy_bypass(self):
        # A proxy is configured, but NO_PROXY makes urllib connect directly:
        # the real peer (loopback) must still be checked.
        env = {'http_proxy': 'http://proxy.invalid:3128', 'no_proxy': '127.0.0.1'}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(netsafe, '_resolve', return_value=['93.184.216.34']):
            with self.assertRaises(netsafe.BlockedURL):
                netsafe.fetch(f'http://127.0.0.1:{self.port}/ok')

    def test_proxied_request_skips_peer_check(self):
        req = urllib.request.Request('http://example.com/x')
        req.set_proxy('127.0.0.1:3128', 'http')
        self.assertTrue(netsafe._via_proxy(req))
        self.assertFalse(netsafe._via_proxy(urllib.request.Request('http://example.com/x')))

    def test_get_text_never_raises(self):
        self.assertEqual(netsafe.get_text('http://10.0.0.1/feed'), '')


if __name__ == '__main__':
    unittest.main()
