"""User accounts, sessions, per-user isolation, and the multi-user pipeline."""
import http.client
import json
import os
import threading
import unittest
from unittest import mock

import _env
import pipeline
import preferences
import server
import settings
import users
from test_ranking import _FakeOllama, feed_xml


def multi_user():
    return mock.patch.object(users, 'AUTH_MODE', 'local')


class UserStoreTests(unittest.TestCase):
    def setUp(self):
        _env.reset()
        self.p = multi_user()
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_password_hash_roundtrip(self):
        h = users.hash_password('correct horse')
        self.assertTrue(users.verify_password('correct horse', h))
        self.assertFalse(users.verify_password('wrong horse', h))
        self.assertFalse(users.verify_password('x', 'garbage'))
        self.assertNotIn('correct horse', h)

    def test_first_user_owns_legacy_data_and_is_admin(self):
        first = users.create_user('Alice', 'password1', role='user')
        self.assertEqual(first['username'], 'alice')
        self.assertEqual(first['role'], 'admin')
        self.assertTrue(first['legacy'])
        second = users.create_user('bob', 'password2')
        self.assertEqual(second['role'], 'user')
        self.assertFalse(second['legacy'])
        self.assertEqual(users.path(os.environ['STATE_FILE'], first), os.environ['STATE_FILE'])
        self.assertEqual(users.path(os.environ['STATE_FILE'], second),
                         os.path.join(users.USERS_DIR, second['id'], 'state.json'))
        with self.assertRaises(ValueError):
            users.create_user('BOB', 'password3')            # case-insensitive duplicate
        with self.assertRaises(ValueError):
            users.create_user('carol', 'short')              # password policy
        with self.assertRaises(ValueError):
            users.create_user('../etc', 'password4')         # username charset

    def test_admin_invariants(self):
        admin = users.create_user('alice', 'password1')
        bob = users.create_user('bob', 'password2')
        with self.assertRaises(ValueError):
            users.update_user(admin['id'], {'role': 'user'})
        with self.assertRaises(ValueError):
            users.update_user(admin['id'], {'disabled': True})
        with self.assertRaises(ValueError):
            users.delete_user(admin['id'])                   # owner can't be deleted
        users.update_user(bob['id'], {'role': 'admin'})
        users.update_user(admin['id'], {'role': 'user'})    # fine now: bob is admin
        os.makedirs(users.user_dir(bob['id']), exist_ok=True)
        users.update_user(admin['id'], {'role': 'admin'})
        users.delete_user(bob['id'])
        self.assertIsNone(users.get(bob['id']))
        self.assertFalse(os.path.exists(users.user_dir(bob['id'])))

    def test_sessions(self):
        u = users.create_user('alice', 'password1')
        self.assertIsNone(users.authenticate('alice', 'nope'))
        self.assertIsNone(users.authenticate('nobody', 'password1'))
        self.assertEqual(users.authenticate('ALICE', 'password1')['id'], u['id'])
        tok = users.create_session(u['id'])
        self.assertEqual(users.session_user(tok)['id'], u['id'])
        self.assertIsNone(users.session_user(tok + 'x'))
        with open(users.SESSIONS_FILE) as fh:
            self.assertNotIn(tok, fh.read(), 'only a hash of the token is stored')
        users.revoke_session(tok)
        self.assertIsNone(users.session_user(tok))
        bob = users.create_user('bob', 'password2')
        tok = users.create_session(bob['id'])
        users.update_user(bob['id'], {'disabled': True})
        self.assertIsNone(users.session_user(tok), 'disabling revokes sessions')
        self.assertIsNone(users.authenticate('bob', 'password2'))

    def test_bootstrap_from_env(self):
        with mock.patch.multiple(users, ADMIN_USERNAME='root', ADMIN_PASSWORD='password1'):
            self.assertIn('created admin', users.bootstrap())
            self.assertIsNone(users.bootstrap())             # only once
        self.assertEqual(users.list_users()[0]['username'], 'root')

    def test_single_user_mode_is_unchanged(self):
        with mock.patch.object(users, 'AUTH_MODE', 'none'):
            self.assertEqual(users.active_users(), [users.OWNER_PSEUDO])
            self.assertEqual(users.path(os.environ['FEEDS_FILE']), os.environ['FEEDS_FILE'])


class AuthServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = server.ThreadingHTTPServer(('127.0.0.1', 0), server.StateHandler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        _env.reset()
        self.p = multi_user()
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def req(self, method, path, body=None, cookie=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        h = {'Content-Type': 'application/json'}
        if cookie:
            h['Cookie'] = f'{users.SESSION_COOKIE}={cookie}'
        h.update(headers or {})
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            data = json.loads(raw)
        except Exception:
            data = raw
        return r.status, data, r

    def login(self, username, password):
        st, body, r = self.req('POST', '/auth/login', {'username': username, 'password': password})
        self.assertEqual(st, 200, body)
        cookie = r.getheader('Set-Cookie')
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)
        return cookie.split(';')[0].split('=', 1)[1]

    def test_setup_then_login_required(self):
        st, me, _ = self.req('GET', '/auth/me')
        self.assertEqual(me, {'mode': 'local', 'user': None, 'needsSetup': True,
                              'localLogin': 'on', 'oidc': None})
        st, _, r = self.req('GET', '/')
        self.assertEqual((st, r.getheader('Location')), (302, '/login'))
        st, body, _ = self.req('GET', '/digest.json')
        self.assertEqual(st, 401)
        st, body, _ = self.req('GET', '/login')
        self.assertEqual(st, 200)
        self.assertIn(b'<form', body)
        # Public assets stay reachable for the login page.
        self.assertEqual(self.req('GET', '/favicon.ico')[0], 200)

        st, body, r = self.req('POST', '/auth/setup', {'username': 'alice', 'password': 'password1'})
        self.assertEqual(st, 201, body)
        self.assertEqual(body['user']['role'], 'admin')
        st, body, _ = self.req('POST', '/auth/setup', {'username': 'eve', 'password': 'password1'})
        self.assertEqual(st, 409, 'setup is one-time only')

        tok = self.login('alice', 'password1')
        st, me, _ = self.req('GET', '/auth/me', cookie=tok)
        self.assertEqual(me['user']['username'], 'alice')
        self.assertEqual(self.req('GET', '/digest.json', cookie=tok)[0], 200)
        st, body, r = self.req('POST', '/auth/logout', cookie=tok)
        self.assertIn('Max-Age=0', r.getheader('Set-Cookie'))
        self.assertEqual(self.req('GET', '/digest.json', cookie=tok)[0], 401)

    def test_login_throttle_and_wrong_password(self):
        users.create_user('alice', 'password1')
        for _ in range(users.LOGIN_MAX_FAILURES):
            st, _, _ = self.req('POST', '/auth/login', {'username': 'alice', 'password': 'bad'})
            self.assertEqual(st, 401)
        st, _, _ = self.req('POST', '/auth/login', {'username': 'alice', 'password': 'password1'})
        self.assertEqual(st, 429)

    def test_cross_site_login_refused(self):
        users.create_user('alice', 'password1')
        st, _, _ = self.req('POST', '/auth/login', {'username': 'alice', 'password': 'password1'},
                            headers={'Origin': 'https://evil.example'})
        self.assertEqual(st, 403)

    def test_per_user_isolation(self):
        users.create_user('alice', 'password1')
        a = self.login('alice', 'password1')
        st, bob, _ = self.req('POST', '/users', {'username': 'bob', 'password': 'password2'}, cookie=a)
        self.assertEqual(st, 201, bob)
        b = self.login('bob', 'password2')

        # Feeds are private.
        with mock.patch.object(server, 'validate_feed_url',
                               return_value={'ok': True, 'title': 'X', 'count': 1}):
            st, body, _ = self.req('POST', '/feeds?skip_validate=1',
                                   {'url': 'https://x.ex/rss', 'source': 'X', 'category': 'tech'},
                                   cookie=b)
        self.assertIn(st, (200, 201), body)
        self.assertEqual([f['url'] for f in self.req('GET', '/feeds', cookie=b)[1]], ['https://x.ex/rss'])
        self.assertEqual(self.req('GET', '/feeds', cookie=a)[1], [])

        # Read state is private.
        st, _, _ = self.req('PUT', '/state', {'readIds': ['r1'], 'later': [], 'history': []}, cookie=b)
        self.assertEqual(st, 200)
        self.assertEqual(self.req('GET', '/state', cookie=b)[1]['readIds'], ['r1'])
        self.assertEqual(self.req('GET', '/state', cookie=a)[1]['readIds'], [])

        # Categories are private; a new user starts from the defaults.
        st, _, _ = self.req('PUT', '/categories',
                            [{'key': 'cooking', 'label': 'Cooking', 'interest': 'recipes'}], cookie=b)
        self.assertEqual(st, 200)
        self.assertEqual([c['key'] for c in self.req('GET', '/categories', cookie=b)[1]], ['cooking'])
        self.assertNotIn('cooking', [c['key'] for c in self.req('GET', '/categories', cookie=a)[1]])

        # Each user has their own digest file.
        self.assertEqual(self.req('GET', '/digest.json', cookie=b)[1]['articles'], [])

    def test_roles(self):
        users.create_user('alice', 'password1')
        users.create_user('bob', 'password2')
        a, b = self.login('alice', 'password1'), self.login('bob', 'password2')
        self.assertEqual(self.req('GET', '/users', cookie=b)[0], 403)
        self.assertEqual(self.req('GET', '/runs', cookie=b)[0], 403)
        st, body, _ = self.req('GET', '/users', cookie=a)
        self.assertEqual(sorted(u['username'] for u in body['users']), ['alice', 'bob'])
        self.assertNotIn('password_hash', json.dumps(body))

        # Settings: a user may change personal keys, not global ones.
        st, body, _ = self.req('GET', '/settings', cookie=b)
        self.assertIsNone(body['deploy'])
        self.assertFalse(body['schema']['ollama_model']['editable'])
        self.assertTrue(body['schema']['feedback_like_weight']['editable'])
        st, body, _ = self.req('PUT', '/settings',
                               {'ollama_model': 'evil:1b', 'feedback_like_weight': 2.5}, cookie=b)
        self.assertEqual(body['settings']['feedback_like_weight'], 2.5)
        self.assertNotEqual(body['settings']['ollama_model'], 'evil:1b')
        # ...and it stays personal.
        self.assertNotEqual(self.req('GET', '/settings', cookie=a)[1]['settings']['feedback_like_weight'], 2.5)
        st, body, _ = self.req('PUT', '/settings', {'ollama_model': 'good:4b'}, cookie=a)
        self.assertEqual(body['settings']['ollama_model'], 'good:4b')
        self.assertEqual(self.req('GET', '/settings', cookie=b)[1]['settings']['ollama_model'], 'good:4b')

    def test_admin_user_management_and_password_change(self):
        alice = users.create_user('alice', 'password1')
        a = self.login('alice', 'password1')
        st, bob, _ = self.req('POST', '/users', {'username': 'bob', 'password': 'password2'}, cookie=a)
        b = self.login('bob', 'password2')
        # Self-protection.
        self.assertEqual(self.req('DELETE', f'/users?id={alice["id"]}', cookie=a)[0], 400)
        self.assertEqual(self.req('PUT', f'/users?id={alice["id"]}', {'disabled': True}, cookie=a)[0], 400)
        # Password change: wrong current refused, right one rotates the session.
        st, _, _ = self.req('PUT', '/auth/password', {'current': 'nope', 'new': 'password3'}, cookie=b)
        self.assertEqual(st, 403)
        st, _, r = self.req('PUT', '/auth/password', {'current': 'password2', 'new': 'password3'}, cookie=b)
        self.assertEqual(st, 200)
        self.assertEqual(self.req('GET', '/state', cookie=b)[0], 401, 'old sessions revoked')
        b = r.getheader('Set-Cookie').split(';')[0].split('=', 1)[1]
        self.assertEqual(self.req('GET', '/state', cookie=b)[0], 200)
        # Admin reset + disable + delete.
        st, _, _ = self.req('PUT', f'/users?id={bob["id"]}', {'disabled': True}, cookie=a)
        self.assertEqual(self.req('GET', '/state', cookie=b)[0], 401)
        st, body, _ = self.req('DELETE', f'/users?id={bob["id"]}', cookie=a)
        self.assertEqual(st, 200)
        self.assertEqual(self.req('DELETE', f'/users?id={bob["id"]}', cookie=a)[0], 404)


class MultiUserPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = server.ThreadingHTTPServer(('127.0.0.1', 0), _FakeOllama)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.host = f'http://127.0.0.1:{cls.srv.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        _env.reset()
        _FakeOllama.peak = _FakeOllama.chat_calls = _FakeOllama.embed_calls = 0
        self.mu = multi_user()
        self.mu.start()
        self.alice = users.create_user('alice', 'password1')
        self.bob = users.create_user('bob', 'password2')
        shared = {'url': 'https://s.ex/rss', 'source': 's', 'category': 'tech'}
        with users.as_user(self.alice):
            server.save_feeds([{'url': 'https://a.ex/rss', 'source': 'a', 'category': 'tech'}, shared])
        with users.as_user(self.bob):
            server.save_feeds([{'url': 'https://b.ex/rss', 'source': 'b', 'category': 'tech'}, shared])
        settings.save({'max_new_scored': 100, 'retain_floor': 0, 'retain_ceiling': 200})
        for u in (self.alice, self.bob):
            with users.as_user(u):
                settings.save({'retain_floor': 0, 'retain_ceiling': 200}, allow_global=False)
        bodies = {'https://a.ex/rss': feed_xml('a', 4), 'https://b.ex/rss': feed_xml('b', 4),
                  'https://s.ex/rss': feed_xml('s', 4)}
        self.patches = [
            mock.patch.object(pipeline, 'OLLAMA_HOST', self.host),
            mock.patch.object(pipeline, 'http_get_text', side_effect=lambda u, *a, **k: bodies.get(u, '')),
            mock.patch.object(pipeline, '_fetch_page', return_value=(None, '')),
            mock.patch.object(pipeline, 'ollama_ps', return_value=[]),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.mu.stop()

    def digest(self, user):
        with users.as_user(user):
            with open(users.path(pipeline.DIGEST_FILE)) as fh:
                return json.load(fh)['articles']

    def test_shared_scoring_private_digests(self):
        self.assertTrue(pipeline.run_once())
        self.assertEqual(_FakeOllama.chat_calls, 12, 'each distinct article is scored once')
        a_src = {x['source'] for x in self.digest(self.alice)}
        b_src = {x['source'] for x in self.digest(self.bob)}
        self.assertEqual(a_src, {'a', 's'})
        self.assertEqual(b_src, {'b', 's'})
        run = pipeline.get_runs()[-1]
        self.assertEqual(run['user_count'], 2)
        for x in self.digest(self.bob):
            self.assertIn('base_score', x)
            self.assertIn('interest_match', x)

        # Bob reads a shared story: it leaves his digest, not Alice's.
        shared_id = next(x['id'] for x in self.digest(self.bob) if x['source'] == 's')
        with users.as_user(self.bob):
            server.save_state({**server.DEFAULT_STATE, 'readIds': [shared_id]})
        _FakeOllama.chat_calls = 0
        self.assertTrue(pipeline.run_once())
        self.assertEqual(_FakeOllama.chat_calls, 0, 'nothing new: everything reused')
        self.assertNotIn(shared_id, {x['id'] for x in self.digest(self.bob)})
        self.assertIn(shared_id, {x['id'] for x in self.digest(self.alice)})

    def test_votes_and_blocklist_are_per_user(self):
        pipeline.run_once()
        target = next(x for x in self.digest(self.alice) if x['source'] == 's' and 'solar' in x['title'])
        with users.as_user(self.alice):
            preferences.set_vote(target['id'], target, 1)
        with users.as_user(self.bob):
            settings.save({'block_keywords': ['solar']}, allow_global=False)
        with users.as_user(self.alice):
            self.assertEqual(len(preferences.feedback()), 1)
        with users.as_user(self.bob):
            self.assertEqual(preferences.feedback(), {})
        pipeline.run_once()
        self.assertTrue(any('solar' in x['title'] for x in self.digest(self.alice)))
        self.assertFalse(any('solar' in x['title'] for x in self.digest(self.bob)))

    def test_disabled_users_are_skipped(self):
        users.update_user(self.bob['id'], {'disabled': True})
        pipeline.run_once()
        self.assertEqual(_FakeOllama.chat_calls, 8, "a disabled user's feeds are not fetched or scored")
        self.assertEqual(pipeline.get_runs()[-1]['user_count'], 1)


if __name__ == '__main__':
    unittest.main()
