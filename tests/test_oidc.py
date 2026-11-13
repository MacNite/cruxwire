"""OIDC single sign-on: JWT verification and the full login flow against a
fake identity provider (in-process, real RSA signatures)."""
import base64
import hashlib
import hmac
import http.client
import http.server
import json
import random
import threading
import time
import unittest
import urllib.parse
from unittest import mock

import _env
import oidc
import server
import users


# ── tiny RSA keygen for tests (stdlib; Miller-Rabin) ─────────────────
def _is_prime(n, rounds=24):
    if n < 4:
        return n in (2, 3)
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    rng = random.Random(n)
    for _ in range(rounds):
        x = pow(rng.randrange(2, n - 2), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits, rng):
    while True:
        c = rng.getrandbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if _is_prime(c):
            return c


def rsa_keypair(seed):
    rng = random.Random(seed)
    e = 65537
    while True:
        p, q = _prime(1024, rng), _prime(1024, rng)
        phi = (p - 1) * (q - 1)
        if p != q and phi % e:
            return p * q, e, pow(e, -1, phi)


def b64(b):
    return oidc.b64url_encode(b)


def int_b64(i):
    return b64(i.to_bytes((i.bit_length() + 7) // 8, 'big'))


KEY = rsa_keypair(1)
OTHER_KEY = rsa_keypair(2)


def sign_rs256(claims, key=KEY, kid='k1', alg='RS256'):
    n, e, d = key
    h = b64(json.dumps({'alg': alg, 'kid': kid, 'typ': 'JWT'}).encode())
    p = b64(json.dumps(claims).encode())
    k = (n.bit_length() + 7) // 8
    t = oidc._DIGEST_INFO['256'] + hashlib.sha256(f'{h}.{p}'.encode()).digest()
    em = b'\x00\x01' + b'\xff' * (k - len(t) - 3) + b'\x00' + t
    return f'{h}.{p}.' + b64(pow(int.from_bytes(em, 'big'), d, n).to_bytes(k, 'big'))


def sign_hs256(claims, secret):
    h = b64(json.dumps({'alg': 'HS256', 'typ': 'JWT'}).encode())
    p = b64(json.dumps(claims).encode())
    return f'{h}.{p}.' + b64(hmac.new(secret.encode(), f'{h}.{p}'.encode(), hashlib.sha256).digest())


class FakeIdP(http.server.BaseHTTPRequestHandler):
    issuer = None
    claims = {}           # next ID token's extra claims
    token_requests = []
    signer = staticmethod(sign_rs256)

    def _json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        cls = type(self)
        if self.path.endswith('/.well-known/openid-configuration'):
            self._json({'issuer': cls.issuer, 'authorization_endpoint': cls.issuer + 'authorize/',
                        'token_endpoint': cls.issuer + 'token/', 'jwks_uri': cls.issuer + 'jwks/'})
        elif self.path.endswith('/jwks/'):
            self._json({'keys': [{'kty': 'RSA', 'kid': 'k1', 'use': 'sig', 'alg': 'RS256',
                                  'n': int_b64(KEY[0]), 'e': int_b64(KEY[1])}]})
        else:
            self._json({'error': 'not_found'}, 404)

    def do_POST(self):
        cls = type(self)
        body = urllib.parse.parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
        cls.token_requests.append({'form': body, 'auth': self.headers.get('Authorization')})
        if body.get('code') != ['good-code']:
            self._json({'error': 'invalid_grant'}, 400)
            return
        claims = {'iss': cls.issuer, 'aud': 'cruxwire', 'exp': time.time() + 300, 'iat': time.time(),
                  **cls.claims}
        self._json({'access_token': 'at', 'token_type': 'Bearer', 'id_token': cls.signer(claims)})

    def log_message(self, *a):
        pass


class JWTTests(unittest.TestCase):
    def setUp(self):
        self.p = mock.patch.multiple(oidc, OIDC_CLIENT_ID='cruxwire', OIDC_CLIENT_SECRET='s3cret')
        self.p.start()
        oidc._jwks.update(keys=[{'kty': 'RSA', 'kid': 'k1', 'n': int_b64(KEY[0]), 'e': int_b64(KEY[1])}],
                          at=time.time())
        self.base = {'iss': 'https://idp/', 'aud': 'cruxwire', 'sub': 'u1', 'nonce': 'n1',
                     'exp': time.time() + 60, 'iat': time.time()}

    def tearDown(self):
        self.p.stop()
        oidc._jwks.update(keys=None, at=0)

    def verify(self, token, nonce='n1'):
        return oidc.verify_id_token(token, nonce, 'https://idp/')

    def test_valid_rs256_and_hs256(self):
        self.assertEqual(self.verify(sign_rs256(self.base))['sub'], 'u1')
        self.assertEqual(self.verify(sign_hs256(self.base, 's3cret'))['sub'], 'u1')

    def test_rejections(self):
        cases = {
            'bad signature': sign_rs256(self.base, key=OTHER_KEY),
            'bad hmac': sign_hs256(self.base, 'wrong'),
            'wrong issuer': sign_rs256({**self.base, 'iss': 'https://evil/'}),
            'wrong audience': sign_rs256({**self.base, 'aud': 'other'}),
            'expired': sign_rs256({**self.base, 'exp': time.time() - 1000}),
            'no sub': sign_rs256({k: v for k, v in self.base.items() if k != 'sub'}),
            'alg none': b64(b'{"alg":"none"}') + '.' + b64(json.dumps(self.base).encode()) + '.',
            'garbage': 'not.a.jwt',
        }
        for name, tok in cases.items():
            with self.subTest(name), self.assertRaises(oidc.OIDCError):
                self.verify(tok)
        with self.assertRaises(oidc.OIDCError):
            self.verify(sign_rs256(self.base), nonce='other')
        # Tampered payload with the original signature.
        h, p, s = sign_rs256(self.base).split('.')
        forged = b64(json.dumps({**self.base, 'sub': 'admin'}).encode())
        with self.assertRaises(oidc.OIDCError):
            self.verify(f'{h}.{forged}.{s}')


class OIDCFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idp = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FakeIdP)
        FakeIdP.issuer = f'http://127.0.0.1:{cls.idp.server_address[1]}/application/o/cruxwire/'
        threading.Thread(target=cls.idp.serve_forever, daemon=True).start()
        cls.srv = server.ThreadingHTTPServer(('127.0.0.1', 0), server.StateHandler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.idp.shutdown()
        cls.srv.shutdown()

    def setUp(self):
        _env.reset()
        oidc._discovery.update(doc=None, at=0)
        oidc._jwks.update(keys=None, at=0)
        oidc._pending.clear()
        FakeIdP.claims = {}
        FakeIdP.token_requests = []
        self.patches = [
            mock.patch.object(users, 'AUTH_MODE', 'oidc'),
            mock.patch.object(users, 'LOCAL_LOGIN', 'admins'),
            mock.patch.multiple(oidc, OIDC_ISSUER=FakeIdP.issuer, OIDC_CLIENT_ID='cruxwire',
                                OIDC_CLIENT_SECRET='s3cret', OIDC_ADMIN_GROUP='cruxwire-admins',
                                OIDC_LINK_BY='none', OIDC_AUTO_CREATE=True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def req(self, method, path, body=None, cookies=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        h = {'Content-Type': 'application/json', **(headers or {})}
        if cookies:
            h['Cookie'] = '; '.join(f'{k}={v}' for k, v in cookies.items())
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            data = json.loads(raw)
        except Exception:
            data = raw
        return r.status, data, r

    @staticmethod
    def set_cookies(r):
        out = {}
        for k, v in r.getheaders():
            if k.lower() == 'set-cookie':
                name, _, rest = v.partition('=')
                out[name] = rest.split(';')[0]
        return out

    def sso_login(self, claims, next_path=None, tamper_state=False, code='good-code'):
        """Run the browser side of the flow; returns (status, location, cookies)."""
        st, _, r = self.req('GET', '/auth/oidc/login' + (f'?next={next_path}' if next_path else ''))
        self.assertEqual(st, 302)
        loc = urllib.parse.urlsplit(r.getheader('Location'))
        q = urllib.parse.parse_qs(loc.query)
        self.assertEqual(q['code_challenge_method'], ['S256'])
        self.assertEqual(q['client_id'], ['cruxwire'])
        self.assertTrue(q['redirect_uri'][0].endswith('/auth/oidc/callback'))
        state_cookie = self.set_cookies(r)[oidc.STATE_COOKIE]
        FakeIdP.claims = {'nonce': q['nonce'][0], **claims}
        state = q['state'][0]
        st, _, r = self.req('GET', f'/auth/oidc/callback?code={code}&state={state}',
                            cookies={oidc.STATE_COOKIE: 'x' if tamper_state else state_cookie})
        return st, r.getheader('Location'), self.set_cookies(r)

    def test_first_sso_login_creates_owner_admin_then_users(self):
        st, me, _ = self.req('GET', '/auth/me')
        self.assertEqual(me['oidc']['label'], 'Sign in with SSO')
        self.assertEqual(me['localLogin'], 'admins')

        st, loc, cookies = self.sso_login({'sub': 'sub-alice', 'preferred_username': 'Alice',
                                           'name': 'Alice A', 'email': 'alice@ex.com',
                                           'email_verified': True, 'groups': []},
                                          next_path='/?view=compact')
        self.assertEqual((st, loc), (302, '/?view=compact'))
        tok = cookies[users.SESSION_COOKIE]
        st, me, _ = self.req('GET', '/auth/me', cookies={users.SESSION_COOKIE: tok})
        self.assertEqual(me['user']['username'], 'alice')
        self.assertTrue(me['user']['sso'])
        self.assertTrue(me['user']['legacy'], 'the first account owns the existing data')
        self.assertEqual(me['user']['role'], 'admin', 'only admin left: never demoted by group sync')
        # PKCE + client auth reached the token endpoint.
        tr = FakeIdP.token_requests[-1]
        self.assertTrue(tr['auth'].startswith('Basic '))
        self.assertIn('code_verifier', tr['form'])

        # Second person: plain user; group membership grants admin, and is re-synced each login.
        st, loc, c2 = self.sso_login({'sub': 'sub-bob', 'preferred_username': 'bob', 'groups': []})
        bob = users.by_oidc(FakeIdP.issuer, 'sub-bob')
        self.assertEqual(bob['role'], 'user')
        self.sso_login({'sub': 'sub-bob', 'preferred_username': 'bob', 'groups': ['cruxwire-admins']})
        self.assertEqual(users.get(bob['id'])['role'], 'admin')
        self.sso_login({'sub': 'sub-bob', 'preferred_username': 'bob', 'groups': []})
        self.assertEqual(users.get(bob['id'])['role'], 'user')
        self.assertEqual(len(users.list_users()), 2, 'same sub → same account')

    def test_username_collision_gets_suffix_not_takeover(self):
        users.create_user('carol', 'password1')
        self.sso_login({'sub': 'sub-x', 'preferred_username': 'carol', 'groups': []})
        u = users.by_oidc(FakeIdP.issuer, 'sub-x')
        self.assertEqual(u['username'], 'carol-2')
        self.assertIsNone(users.by_username('carol').get('oidc_sub'))

    def test_link_by_verified_email(self):
        admin = users.create_user('root', 'password1')
        with users._lock:
            data = users._load()
            data[0]['email'] = 'root@ex.com'
            users._save(data)
        with mock.patch.object(oidc, 'OIDC_LINK_BY', 'email'):
            self.sso_login({'sub': 'sub-r', 'email': 'root@ex.com', 'email_verified': False,
                            'preferred_username': 'r', 'groups': ['cruxwire-admins']})
            self.assertIsNone(users.get(admin['id']).get('oidc_sub'), 'unverified email never links')
            self.sso_login({'sub': 'sub-r2', 'email': 'root@ex.com', 'email_verified': True,
                            'preferred_username': 'r2', 'groups': ['cruxwire-admins']})
            self.assertEqual(users.get(admin['id'])['oidc_sub'], 'sub-r2')

    def test_failures_redirect_to_login_with_error(self):
        st, loc, cookies = self.sso_login({'sub': 's'}, tamper_state=True)
        self.assertEqual(st, 302)
        self.assertTrue(loc.startswith('/login?error='))
        self.assertNotIn(users.SESSION_COOKIE, cookies)
        st, loc, cookies = self.sso_login({'sub': 's'}, code='bad-code')
        self.assertIn('Identity+provider+error', loc)
        st, _, r = self.req('GET', '/auth/oidc/callback?error=access_denied&error_description=nope')
        self.assertIn('nope', r.getheader('Location'))
        # A used state can't be replayed (the failed-code attempt consumed it);
        # only the tampered attempt's state is still pending until it expires.
        self.assertEqual(len(oidc._pending), 1)

    def test_state_replay_refused(self):
        st, _, r = self.req('GET', '/auth/oidc/login')
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(r.getheader('Location')).query)
        state = q['state'][0]
        cookie = {oidc.STATE_COOKIE: self.set_cookies(r)[oidc.STATE_COOKIE]}
        FakeIdP.claims = {'nonce': q['nonce'][0], 'sub': 'sub-z', 'groups': []}
        st, _, r = self.req('GET', f'/auth/oidc/callback?code=good-code&state={state}', cookies=cookie)
        self.assertIn(users.SESSION_COOKIE, self.set_cookies(r))
        st, _, r = self.req('GET', f'/auth/oidc/callback?code=good-code&state={state}', cookies=cookie)
        self.assertIn('expired', r.getheader('Location'))
        self.assertNotIn(users.SESSION_COOKIE, self.set_cookies(r))

    def test_open_redirect_blocked(self):
        for bad in ('//evil.example/', 'https://evil.example/', '/\\evil'):
            st, loc, _ = self.sso_login({'sub': 'sub-n', 'groups': []},
                                        next_path=urllib.parse.quote(bad, safe=''))
            self.assertEqual(loc, '/')

    def test_auto_create_off_and_disabled(self):
        with mock.patch.object(oidc, 'OIDC_AUTO_CREATE', False):
            st, loc, cookies = self.sso_login({'sub': 'nobody'})
            self.assertIn('No+Cruxwire+account', loc)
        self.sso_login({'sub': 'sub-a', 'preferred_username': 'a', 'groups': ['cruxwire-admins']})
        self.sso_login({'sub': 'sub-d', 'preferred_username': 'd', 'groups': []})
        d = users.by_oidc(FakeIdP.issuer, 'sub-d')
        users.update_user(d['id'], {'disabled': True})
        st, loc, cookies = self.sso_login({'sub': 'sub-d', 'preferred_username': 'd', 'groups': []})
        self.assertIn('disabled', loc)
        self.assertNotIn(users.SESSION_COOKIE, cookies)

    def test_local_login_policy(self):
        users.create_user('root', 'password1')          # admin (first)
        users.create_user('pat', 'password2')           # plain user
        self.assertEqual(self.req('POST', '/auth/login', {'username': 'root', 'password': 'password1'})[0], 200)
        self.assertEqual(self.req('POST', '/auth/login', {'username': 'pat', 'password': 'password2'})[0], 401,
                         'LOCAL_LOGIN=admins: break-glass for admins only')
        with mock.patch.object(users, 'LOCAL_LOGIN', 'off'):
            self.assertEqual(self.req('POST', '/auth/login', {'username': 'root', 'password': 'password1'})[0], 403)

    def test_disabled_when_not_configured(self):
        with mock.patch.object(oidc, 'OIDC_ISSUER', ''):
            self.assertEqual(self.req('GET', '/auth/oidc/login')[0], 404)
            self.assertIsNone(self.req('GET', '/auth/me')[1]['oidc'])


if __name__ == '__main__':
    unittest.main()
