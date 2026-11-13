"""Regression tests for the findings of the November 2026 code review
(numbers refer to that review). Each test fails on the pre-fix code."""
import http.client
import http.server
import json
import os
import socket
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import _env
import article_ids
import corpus_archive
import pipeline
import preferences
import server
import settings
import users
from test_ranking import _FakeOllama, feed_xml
from test_server import digest_articles


def _load_json(path):
    with open(path) as fh:
        return json.load(fh)


def multi_user():
    return mock.patch.object(users, 'AUTH_MODE', 'local')


# ── #2 article ids ─────────────────────────────────────────────────────
class ArticleIdTests(unittest.TestCase):
    def setUp(self):
        _env.reset()

    def test_legacy_collision_is_gone(self):
        a, b = 'https://example.test/Aa', 'https://example.test/BB'
        self.assertEqual(article_ids.legacy_id(a), article_ids.legacy_id(b))
        self.assertNotEqual(pipeline.stable_id(a), pipeline.stable_id(b))
        self.assertRegex(pipeline.stable_id(a), r'^[0-9a-f]{16}$')

    def test_migration_rewrites_every_store(self):
        url, gone = 'https://pub.ex/story', 'https://pub.ex/aged-out'
        old, new = article_ids.legacy_id(url), article_ids.stable_id(url)
        with open(os.environ['DIGEST_FILE'], 'w') as fh:
            json.dump({'articles': [{'id': old, 'url': url, 'title': 't', 'cluster_id': old}]}, fh)
        with open(os.environ['EMBEDDINGS_FILE'], 'w') as fh:
            json.dump({'model': 'm', 'vectors': {old: [1.0, 0.0]}}, fh)
        with open(os.environ['STATE_FILE'], 'w') as fh:
            json.dump({'readIds': [old, article_ids.legacy_id(gone)],
                       'later': [{'id': old, 'url': url}], 'history': [{'id': old, 'url': url}]}, fh)
        with open(os.environ['PREFERENCES_FILE'], 'w') as fh:
            json.dump({'feedback': {old: {'vote': 1, 'url': url}},
                       'onboarding': {'status': 'completed', 'skipped': [old]}}, fh)

        server.migrate_article_ids()
        load = lambda v: _load_json(os.environ[v])
        d = load('DIGEST_FILE')['articles'][0]
        self.assertEqual((d['id'], d['cluster_id']), (new, new))
        self.assertEqual(list(load('EMBEDDINGS_FILE')['vectors']), [new])
        st = load('STATE_FILE')
        self.assertEqual(st['readIds'][0], new)
        self.assertEqual(st['readIds'][1], article_ids.legacy_id(gone))   # no URL known: kept
        self.assertEqual(st['later'][0]['id'], new)
        self.assertEqual(st['history'][0]['id'], new)
        p = load('PREFERENCES_FILE')
        self.assertEqual(list(p['feedback']), [new])
        self.assertEqual(p['onboarding']['skipped'], [new])
        # Idempotent.
        self.assertEqual(article_ids.migrate_current_user(
            server.DIGEST_FILE, pipeline.EMBEDDINGS_FILE, server.STATE_FILE,
            preferences.PREFERENCES_FILE), 0)

    def test_migration_never_remaps_a_mismatched_id(self):
        # An 8-hex id that is NOT the legacy hash of its URL is left alone.
        with open(os.environ['STATE_FILE'], 'w') as fh:
            json.dump({'readIds': ['deadbeef'],
                       'later': [{'id': 'deadbeef', 'url': 'https://pub.ex/x'}]}, fh)
        server.migrate_article_ids()
        st = _load_json(os.environ['STATE_FILE'])
        self.assertEqual(st['later'][0]['id'], 'deadbeef')

    def test_pooled_record_requires_same_url(self):
        # Even with an id collision, a user's fresh article never picks up a
        # pooled record for a different URL.
        user = users.OWNER_PSEUDO
        fresh = [{'id': 'x1', 'url': 'https://bob.ex/BB', 'title': 'bob', '_feeds': {'f'},
                  'published_at': datetime.now(timezone.utc).isoformat()}]
        pool = {'x1': {'id': 'x1', 'url': 'https://alice.ex/Aa', 'title': 'alice story',
                       'score': 9, 'category': 'tech', 'cluster_id': 'x1',
                       'published_at': datetime.now(timezone.utc).isoformat()}}
        inp = {'user': user, 'feed_urls': {'f'}, 'feed_cats': {}, 'read_ids': set(),
               'blocklist': [], 'prev': [], 'emb_index': {}}
        with users.as_user(user):
            arts, _ = pipeline.build_user_digest(inp, fresh, pool, 'm', None, False)
        self.assertEqual(arts, [])


# ── #3 / #4 first-admin setup ──────────────────────────────────────────
class SetupTests(unittest.TestCase):
    def setUp(self):
        _env.reset()
        self.p = multi_user()
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_first_admin_is_atomic(self):
        users.create_first_admin('alice', 'password1')
        with self.assertRaises(users.SetupClosed):
            users.create_first_admin('mallory', 'password1')
        self.assertEqual([u['username'] for u in users.list_users()], ['alice'])

    def test_concurrent_setup_creates_one_admin(self):
        barrier = threading.Barrier(8)
        results = []

        def go(i):
            barrier.wait()
            try:
                users.create_first_admin(f'user{i}', 'password1')
                results.append('ok')
            except users.SetupClosed:
                results.append('closed')

        ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(results.count('ok'), 1)
        self.assertEqual(len(users.list_users()), 1)

    def test_corrupt_store_fails_closed(self):
        users.create_first_admin('alice', 'password1')
        with open(users.USERS_FILE, 'w') as fh:
            fh.write('{not json')
        with self.assertRaises(users.AccountStoreError):
            users.needs_setup()
        with self.assertRaises(users.AccountStoreError):
            users.create_first_admin('mallory', 'password1')

    def test_missing_store_after_setup_fails_closed(self):
        users.create_first_admin('alice', 'password1')
        os.unlink(users.USERS_FILE)
        for f in (users.USERS_BACKUP_FILE,):
            if os.path.exists(f):
                os.unlink(f)
        with self.assertRaises(users.AccountStoreError):
            users.needs_setup()

    def test_fresh_install_still_needs_setup(self):
        self.assertTrue(users.needs_setup())


class _ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = server.BoundedThreadingHTTPServer(('127.0.0.1', 0), server.StateHandler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        h = {'Content-Type': 'application/json'}
        h.update(headers or {})
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        try:
            return r.status, json.loads(data), r
        except Exception:
            return r.status, data, r


class AuthServerFixTests(_ServerCase):
    def setUp(self):
        _env.reset()
        self.p = multi_user()
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_delayed_setup_request_gets_409(self):
        # The request passed the needs_setup() pre-check, then someone else
        # finished setup before it created its account.
        users.create_first_admin('alice', 'password1')
        with mock.patch.object(users, 'needs_setup', return_value=True):
            st, body, _ = self.req('POST', '/auth/setup', {'username': 'mallory', 'password': 'password1'})
        self.assertEqual(st, 409)
        self.assertEqual(len(users.list_users()), 1)

    def test_corrupt_store_returns_503(self):
        users.create_first_admin('alice', 'password1')
        with open(users.USERS_FILE, 'w') as fh:
            fh.write('garbage')
        st, _, _ = self.req('GET', '/auth/me')
        self.assertEqual(st, 503)
        st, _, _ = self.req('POST', '/auth/setup', {'username': 'mallory', 'password': 'password1'})
        self.assertEqual(st, 503)

    def _login(self, name):
        st, _, r = self.req('POST', '/auth/login', {'username': name, 'password': 'password1'})
        self.assertEqual(st, 200)
        return r.getheader('Set-Cookie').split(';')[0]

    def test_account_header_mismatch_is_refused(self):
        alice = users.create_first_admin('alice', 'password1')
        bob = users.create_user('bob', 'password1')
        cookie = self._login('bob')
        # A tab loaded for alice now carries bob's cookie.
        st, body, _ = self.req('PUT', '/state', {'later': []},
                               {'Cookie': cookie, 'X-Cruxwire-Account': alice['id']})
        self.assertEqual(st, 409)
        self.assertTrue(body.get('accountChanged'))
        st, _, _ = self.req('GET', '/state', headers={'Cookie': cookie, 'X-Cruxwire-Account': bob['id']})
        self.assertEqual(st, 200)


# ── #11 pagination, #13 limits, validation ─────────────────────────────
class ServerFixTests(_ServerCase):
    def setUp(self):
        _env.reset()
        server._rate_hits.clear()
        arts = digest_articles(30)
        with open(os.environ['DIGEST_FILE'], 'w') as fh:
            json.dump({'generated_at': '2026-11-13T05:00:00Z', 'articles': arts}, fh)
        with open(os.environ['FEEDS_FILE'], 'w') as fh:
            json.dump([], fh)

    def test_exclude_paging_does_not_skip_after_reading(self):
        st, d, _ = self.req('GET', '/articles?limit=15&exclude=')
        first = [a['id'] for a in d['articles']]
        self.assertEqual(len(first), 15)
        self.req('PUT', '/state', {'readIds': first})
        st, d, _ = self.req('GET', '/articles?limit=15&exclude=' + ','.join(first))
        self.assertEqual(len(d['articles']), 15)
        self.assertFalse(set(first) & {a['id'] for a in d['articles']})
        self.assertIsNone(d['next_offset'])

    def test_malformed_state_payload_is_not_fatal(self):
        st, _, _ = self.req('PUT', '/state', {'readIds': 5, 'later': 'abc',
                                              'history': [{'id': 1}, {'id': 'ok', 'title': 'x'}],
                                              'sourceStats': {'s': {'opens': 'nan'}}})
        self.assertEqual(st, 200)
        st, d, _ = self.req('GET', '/state')
        self.assertEqual(d['later'], [])
        self.assertEqual([h['id'] for h in d['history']], ['ok'])
        self.assertEqual(d['sourceStats']['s']['opens'], 0.0)

    def test_search_rate_limit(self):
        with mock.patch.object(server, 'SEARCH_PER_MINUTE', 2), \
             mock.patch.object(pipeline, 'semantic_search', return_value=[]):
            codes = [self.req('GET', '/search?q=x')[0] for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_body_deadline(self):
        with mock.patch.object(server, 'BODY_READ_DEADLINE_S', 0.3):
            s = socket.create_connection(('127.0.0.1', self.port), timeout=5)
            s.sendall(b'PUT /state HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n'
                      b'Content-Length: 100\r\n\r\n{')
            time.sleep(0.5)
            try:
                s.sendall(b' ')
            except OSError:
                pass
            try:
                data = s.recv(200)
            finally:
                s.close()
        self.assertIn(b' 408 ', data)

    def test_concurrency_cap_returns_503(self):
        srv = server.BoundedThreadingHTTPServer(('127.0.0.1', 0), server.StateHandler, max_concurrent=1)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            hog = socket.create_connection(('127.0.0.1', port))   # holds the only slot
            time.sleep(0.2)
            c = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            c.request('GET', '/status')
            self.assertEqual(c.getresponse().status, 503)
            hog.close()
        finally:
            srv.shutdown()


# ── #8 #9 #10 #12 pipeline ─────────────────────────────────────────────
class PipelineFixTests(unittest.TestCase):
    def setUp(self):
        _env.reset()

    def test_embedding_index_ignored_for_other_model(self):
        with open(os.environ['EMBEDDINGS_FILE'], 'w') as fh:
            json.dump({'model': 'old-model', 'vectors': {'a': [1.0, 0.0]}}, fh)
        self.assertEqual(pipeline._load_embeddings_index('new-model'), {})
        self.assertEqual(pipeline._load_embeddings_index('old-model'), {'a': [1.0, 0.0]})

    def test_search_skips_other_dimensions(self):
        with open(os.environ['EMBEDDINGS_FILE'], 'w') as fh:
            json.dump({'model': settings.load()['embed_model'],
                       'vectors': {'short': [1.0, 0.0], 'ok': [1.0, 0.0, 0.0]}}, fh)
        with mock.patch.object(pipeline, '_embed', return_value=[1.0, 0.0, 0.0]):
            res = pipeline.semantic_search('q', threshold=0.1)
        self.assertEqual([r['id'] for r in res], ['ok'])

    def test_malformed_items_do_not_abort_feed(self):
        now = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
        xml = ('<rss><channel>'
               f'<item><title>bad entity &#99999999999;</title><link>https://ok.ex/1</link><pubDate>{now}</pubDate></item>'
               f'<item><title>bad url</title><link>http://[</link><pubDate>{now}</pubDate></item>'
               f"<item><title>quote</title><link>https://ok.ex/a'b c</link><pubDate>{now}</pubDate></item>"
               f'<item><title>good</title><link>https://ok.ex/2</link><pubDate>{now}</pubDate></item>'
               '</channel></rss>')
        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        out = pipeline.parse_feed(xml, 'src', 'tech', cutoff)
        urls = [a['url'] for a in out]
        self.assertIn('https://ok.ex/2', urls)
        self.assertIn('https://ok.ex/1', urls)
        self.assertIn('https://ok.ex/a%27b%20c', urls)
        self.assertNotIn('http://[', urls)

    def test_semantic_block_survives_embedding_outage(self):
        arts = [{'id': 'a', 'title': 't', 'embedding': [1.0, 0.0]}]
        with mock.patch.object(pipeline, '_embed', return_value=None):
            self.assertEqual(pipeline.semantic_block(arts, ['politics'], 0.5, 'm'), arts)

    def test_archive_writes_in_same_second_do_not_collide(self):
        d = os.path.join(_env.DATA, 'corpus')
        fixed = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed

        with mock.patch.object(corpus_archive, 'CAPTURE_ENABLED', True), \
             mock.patch.object(corpus_archive, 'CORPUS_DIR', d), \
             mock.patch.object(corpus_archive, 'datetime', _DT):
            corpus_archive.archive_articles([{'id': 'a', 'title': 'alice'}], {}, 'm', {}, log=lambda *_: None, owner='alice')
            corpus_archive.archive_articles([{'id': 'b', 'title': 'bob'}], {}, 'm', {}, log=lambda *_: None, owner='bob')
        rows = []
        for root, _, files in os.walk(d):
            for f in files:
                with open(os.path.join(root, f)) as fh:
                    rows += [json.loads(line) for line in fh]
        self.assertEqual(sorted(r['owner'] for r in rows), ['alice', 'bob'])


# ── #14 failed scoring is retried ──────────────────────────────────────
class RescoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _FakeOllama)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.host = f'http://127.0.0.1:{cls.srv.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        _env.reset()
        _FakeOllama.chat_calls = 0
        with open(os.environ['FEEDS_FILE'], 'w') as fh:
            json.dump([{'url': 'https://a.ex/rss', 'source': 'a', 'category': 'tech'}], fh)
        settings.save({'max_new_scored': 10, 'retain_floor': 0, 'retain_ceiling': 200})
        bodies = {'https://a.ex/rss': feed_xml('a', 4)}
        self.patches = [
            mock.patch.object(pipeline, 'http_get_text', side_effect=lambda u, *a, **k: bodies.get(u, '')),
            mock.patch.object(pipeline, '_fetch_page', return_value=(None, '')),
            mock.patch.object(pipeline, 'ollama_ps', return_value=[]),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_fallback_scores_are_retried_when_ollama_returns(self):
        with mock.patch.object(pipeline, 'OLLAMA_HOST', 'http://127.0.0.1:9'):
            self.assertTrue(pipeline.run_once())
        d = _load_json(os.environ['DIGEST_FILE'])['articles']
        self.assertTrue(d and all(a['score_ok'] is False for a in d))

        with mock.patch.object(pipeline, 'OLLAMA_HOST', self.host):
            self.assertTrue(pipeline.run_once())
        self.assertEqual(_FakeOllama.chat_calls, 4)
        d = _load_json(os.environ['DIGEST_FILE'])['articles']
        self.assertTrue(all(a['score_ok'] is True for a in d))
        self.assertTrue(all(a['summary'] == 'fake summary' for a in d))

        # A third run with Ollama healthy reuses them (no new chat calls).
        _FakeOllama.chat_calls = 0
        with mock.patch.object(pipeline, 'OLLAMA_HOST', self.host):
            pipeline.run_once()
        self.assertEqual(_FakeOllama.chat_calls, 0)


if __name__ == '__main__':
    unittest.main()
