"""Regression tests for the Nov 2026 code-review findings."""
import io
import json
import math
import os
import re
import threading
import unittest
from datetime import datetime, timezone
from unittest import mock

import _env
import corpus_archive
import ids
import netsafe
import pipeline
import preferences
import server
import users

CUTOFF = datetime(2020, 1, 1, tzinfo=timezone.utc)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AccountSetupTests(unittest.TestCase):
    def setUp(self):
        _env.reset()
        self.p = mock.patch.object(users, 'AUTH_MODE', 'local')
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_concurrent_first_admin_setup_creates_exactly_one(self):
        barrier = threading.Barrier(8)
        results = []

        def attempt(i):
            barrier.wait()
            try:
                users.create_user(f'admin{i}', 'password1', 'admin', first_only=True)
                results.append('ok')
            except users.SetupComplete:
                results.append('refused')

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count('ok'), 1)
        self.assertEqual(len(users.list_users()), 1)

    def test_corrupt_store_fails_closed(self):
        users.create_user('alice', 'password1')
        with open(users.USERS_FILE, 'w') as fh:
            fh.write('{not json')
        self.assertFalse(users.needs_setup(), 'damage must not reopen setup')
        with self.assertRaises(users.StoreCorrupt):
            users.create_user('mallory', 'password1', 'admin', first_only=True)
        self.assertIsNone(users.authenticate('alice', 'password1'))
        self.assertIn('WARNING', users.bootstrap())

    def test_wrong_shape_store_is_corrupt(self):
        with open(users.USERS_FILE, 'w') as fh:
            json.dump({'version': 1}, fh)
        self.assertFalse(users.needs_setup())

    def test_missing_store_is_fresh_install(self):
        self.assertTrue(users.needs_setup())


class ArticleIdTests(unittest.TestCase):
    def test_reported_collision_is_gone(self):
        self.assertEqual(ids.legacy_id('https://x.ex/Aa'), ids.legacy_id('https://x.ex/BB'))
        self.assertNotEqual(pipeline.stable_id('https://x.ex/Aa'), pipeline.stable_id('https://x.ex/BB'))
        self.assertEqual(len(pipeline.stable_id('https://x.ex/a')), 16)

    def test_legacy_read_markers_still_match(self):
        url = 'https://x.ex/story'
        art = {'id': pipeline.stable_id(url), 'url': url}
        self.assertTrue(pipeline.is_read(art, {ids.legacy_id(url)}))
        self.assertFalse(pipeline.is_read(art, {'deadbeef'}))

    def test_migrate_id_only_upgrades_matching_legacy_ids(self):
        url = 'https://x.ex/story'
        self.assertEqual(ids.migrate_id(ids.legacy_id(url), url), ids.article_id(url))
        self.assertEqual(ids.migrate_id('a01', url), 'a01')            # not a legacy id
        self.assertEqual(ids.migrate_id('deadbeef', url), 'deadbeef')  # legacy, other URL

    def test_votes_and_later_are_rekeyed(self):
        _env.reset()
        url = 'https://x.ex/voted'
        with open(os.environ['PREFERENCES_FILE'], 'w') as fh:
            json.dump({'feedback': {ids.legacy_id(url): {'vote': 1, 'url': url, 'title': 't'}}}, fh)
        self.assertIn(ids.article_id(url), preferences.votes_map())
        with open(os.environ['STATE_FILE'], 'w') as fh:
            json.dump({'later': [{'id': ids.legacy_id(url), 'url': url, 'title': 't'}]}, fh)
        self.assertEqual(server.load_state()['later'][0]['id'], ids.article_id(url))


class FeedParsingTests(unittest.TestCase):
    def test_invalid_numeric_entity_does_not_raise(self):
        self.assertIn('�', pipeline.clean_text('a&#99999999999;b'))
        xml = ('<rss><item><title>T</title><link>https://x.ex/1&#99999999999;</link>'
               '<pubDate>Fri, 13 Nov 2026 05:00:00 GMT</pubDate></item></rss>')
        pipeline.parse_feed(xml, 'S', 'tech', CUTOFF)    # must not raise

    def test_unsafe_links_are_dropped(self):
        for link in ("javascript:alert(1)", "https://x.ex/a'onmouseover='x",
                     'https://x.ex/"><script>', 'http://', 'ftp://x.ex/a'):
            xml = (f'<rss><item><title>T</title><link>{link}</link>'
                   '<pubDate>Fri, 13 Nov 2026 05:00:00 GMT</pubDate></item></rss>')
            self.assertEqual(pipeline.parse_feed(xml, 'S', 'tech', CUTOFF), [], link)

    def test_one_bad_item_does_not_drop_the_feed(self):
        xml = ''.join(f'<item><title>T{i}</title><link>https://x.ex/{i}</link>'
                      '<pubDate>Fri, 13 Nov 2026 05:00:00 GMT</pubDate></item>' for i in range(3))
        real = pipeline._item
        calls = {'n': 0}

        def flaky(*a, **k):
            calls['n'] += 1
            if calls['n'] == 2:
                raise ValueError('boom')
            return real(*a, **k)

        with mock.patch.object(pipeline, '_item', side_effect=flaky):
            out = pipeline.parse_feed(f'<rss>{xml}</rss>', 'S', 'tech', CUTOFF)
        self.assertEqual([a['title'] for a in out], ['T0', 'T2'])


class EmbeddingIndexTests(unittest.TestCase):
    def setUp(self):
        _env.reset()

    def _write(self, model, vectors):
        with open(os.environ['EMBEDDINGS_FILE'], 'w') as fh:
            json.dump({'model': model, 'vectors': vectors}, fh)

    def test_index_from_other_model_is_ignored(self):
        self._write('old-embed', {'a': [1.0, 0.0]})
        self.assertEqual(pipeline._load_embeddings_index('new-embed'), {})
        self.assertEqual(pipeline._load_embeddings_index('old-embed'), {'a': [1.0, 0.0]})

    def test_mixed_dimensions_are_filtered(self):
        self._write('m', {'a': [1.0, 0.0], 'b': [1.0, 0.0, 0.0], 'c': []})
        self.assertEqual(pipeline._load_embeddings_index('m'), {'a': [1.0, 0.0]})


class StateValidationTests(unittest.TestCase):
    def test_non_list_fields_are_rejected(self):
        for field in ('readIds', 'later', 'history'):
            with self.assertRaises(ValueError):
                server.normalize_payload({field: 123})

    def test_non_finite_counters_become_zero(self):
        st = server.normalize_payload({'sourceStats': {'S': {'opens': 'NaN', 'saves': float('inf'),
                                                              'dismisses': True}}})
        self.assertEqual(st['sourceStats']['S'], {'opens': 0.0, 'saves': 0.0, 'dismisses': 0.0})
        json.dumps(st, allow_nan=False)

    def test_junk_list_entries_are_dropped(self):
        st = server.normalize_payload({'readIds': ['ok', 5, None, 'x' * 100],
                                       'later': [{'id': ['x']}, 'nope']})
        self.assertEqual(st['readIds'], ['ok'])
        self.assertEqual(server.prune(st)['later'], [])


class StateValidationHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = server.BoundedThreadingHTTPServer(('127.0.0.1', 0), server.StateHandler,
                                                    max_connections=4)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        _env.reset()

    def test_put_state_with_numeric_readids_is_400(self):
        import http.client
        for _ in range(6):    # more requests than slots: slots must be released
            c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
            c.request('PUT', '/state', body=json.dumps({'readIds': 5}),
                      headers={'Content-Type': 'application/json'})
            r = c.getresponse()
            r.read()
            c.close()
            self.assertEqual(r.status, 400)


class NetsafeDeadlineTests(unittest.TestCase):
    def test_slow_drip_hits_total_deadline(self):
        class Drip:
            def read(self, n):
                return b'x'
        with mock.patch.object(netsafe.time, 'monotonic', side_effect=[0, 0, 0, 100]):
            with self.assertRaises(TimeoutError):
                netsafe._read_capped(Drip(), 1000, deadline=10)

    def test_reads_until_eof_or_limit(self):
        self.assertEqual(netsafe._read_capped(io.BytesIO(b'abcdef'), 4, 1e18), b'abcd')
        self.assertEqual(netsafe._read_capped(io.BytesIO(b'ab'), 4, 1e18), b'ab')


class CorpusArchiveTests(unittest.TestCase):
    def test_same_second_writes_do_not_overwrite(self):
        d = os.path.join(_env.DATA, 'corpus')
        with mock.patch.object(corpus_archive, 'CAPTURE_ENABLED', True), \
                mock.patch.object(corpus_archive, 'CORPUS_DIR', d):
            for owner in ('alice', 'bob'):
                corpus_archive.archive_articles([{'id': owner, 'title': owner}], {}, 'm', {},
                                                log=lambda *_: None, owner=owner)
        files = [os.path.join(r, f) for r, _, fs in os.walk(d) for f in fs if f.endswith('.jsonl')]
        self.assertEqual(len(files), 2)


class FrontendInjectionContractTests(unittest.TestCase):
    """Feed-supplied URLs and text must never be interpolated into inline JS."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, 'digest.html'), encoding='utf-8') as fh:
            cls.html = fh.read()

    def test_no_url_inside_inline_handlers(self):
        handlers = re.findall(r'on[a-z]+="([^"]*)"', self.html)
        bad = [h for h in handlers if re.search(r"\$\{[^}]*\burl\b", h)]
        self.assertEqual(bad, [])

    def test_ids_in_handlers_go_through_jsid(self):
        handlers = re.findall(r'on[a-z]+="([^"]*)"', self.html)
        bad = [h for h in handlers if re.search(r"'\$\{(?!jsId\()[^}]*\.id\}'", h)]
        self.assertEqual(bad, [])

    def test_hrefs_use_hrefattr(self):
        bad = re.findall(r'href="\$\{(?!hrefAttr\()[^}]*\}"', self.html)
        self.assertEqual(bad, [])


if __name__ == '__main__':
    unittest.main()
