import http.client
import json
import os
import threading
import unittest
from unittest import mock

import _env
import onboarding
import pipeline
import preferences
import server


def digest_articles(n=30):
    cats = ['tech', 'ai', 'gaming']
    return [{'id': f'a{i:02d}', 'title': f'Story {i}', 'url': f'https://pub.ex/{i}', 'source': f'S{i % 4}',
             'category': cats[i % 3], 'score': 9 - i * 0.1, 'summary': 's', 'image': None,
             'published_at': '2026-11-13T05:00:00+00:00', 'cluster_id': f'a{i:02d}',
             'cluster_rep': True, 'cluster_size': 1} for i in range(n)]


class ServerTests(unittest.TestCase):
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
        with open(os.environ['DIGEST_FILE'], 'w') as fh:
            json.dump({'generated_at': '2026-11-13T05:00:00Z', 'articles': digest_articles()}, fh)
        with open(os.environ['FEEDS_FILE'], 'w') as fh:
            json.dump([], fh)

    def req(self, method, path, body=None, headers=None, raw=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        h = {'Content-Type': 'application/json'}
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        payload = r.read()
        c.close()
        try:
            return r.status, json.loads(payload), r
        except Exception:
            return r.status, payload, r

    # ── static / contract ──────────────────────────────────────────
    def test_fallback_svgs_served_and_traversal_refused(self):
        st, body, r = self.req('GET', '/static/fallback/energy.svg')
        self.assertEqual(st, 200)
        self.assertEqual(r.getheader('Content-Type'), 'image/svg+xml')
        self.assertIn(b'<svg', body)
        for bad in ['/static/fallback/../server.py', '/static/fallback/nope.svg', '/static/x.svg']:
            self.assertEqual(self.req('GET', bad)[0], 404, bad)

    def test_html_contract(self):
        st, body, _ = self.req('GET', '/')
        html = body.decode()
        # Onboarding cards always have a fixed 16:9 media box with a skeleton,
        # and images fall back to the local illustration on error.
        for needle in ['id="view-onboarding"', 'class="ob-media"', 'aspect-ratio: 16 / 9',
                       'ob-skeleton', 'fallback_image', "view=compact", 'Discover more',
                       'Why this story', 'aria-label="Like"', 'aria-label="Dislike"']:
            self.assertIn(needle, html, needle)

    def test_no_wildcard_cors_by_default(self):
        _, _, r = self.req('GET', '/feeds')
        self.assertIsNone(r.getheader('Access-Control-Allow-Origin'))
        self.assertEqual(r.getheader('X-Content-Type-Options'), 'nosniff')

    # ── security ──────────────────────────────────────────────────
    def test_cross_origin_mutation_refused(self):
        st, _, _ = self.req('POST', '/refresh', headers={'Origin': 'https://evil.example'})
        self.assertEqual(st, 403)
        st, _, _ = self.req('PUT', '/state', {'readIds': []}, headers={'Origin': 'https://evil.example'})
        self.assertEqual(st, 403)

    def test_same_origin_allowed(self):
        st, _, _ = self.req('POST', '/feedback', {'id': 'a01', 'vote': 1},
                            headers={'Origin': f'http://127.0.0.1:{self.port}'})
        self.assertEqual(st, 200)

    def test_body_size_limits(self):
        big = json.dumps({'id': 'a01', 'vote': 1, 'pad': 'x' * 70000}).encode()
        self.assertEqual(self.req('POST', '/feedback', raw=big)[0], 413)
        self.assertEqual(self.req('POST', '/feedback', raw=b'{bad json')[0], 400)
        self.assertEqual(self.req('POST', '/feedback', raw=b'[1,2]')[0], 400)

    def test_onboarding_sources_block_private(self):
        st, body, _ = self.req('POST', '/onboarding/sources', {'url': 'http://192.168.0.10/rss'})
        self.assertEqual(st, 400)
        self.assertIn('Blocked', body['error'])
        st, body, _ = self.req('POST', '/onboarding/seeds', {'urls': ['http://169.254.169.254/']})
        self.assertFalse(body['results'][0]['ok'])

    # ── feedback API ──────────────────────────────────────────────
    def test_feedback_by_server_known_id_only(self):
        self.assertEqual(self.req('POST', '/feedback', {'id': 'zzz', 'vote': 1})[0], 404)
        self.assertEqual(self.req('POST', '/feedback', {'id': 'a01', 'vote': 5})[0], 400)
        self.assertEqual(self.req('POST', '/feedback', {'id': 'a01', 'vote': True})[0], 400)
        # client-supplied title/score are ignored
        st, body, _ = self.req('POST', '/feedback', {'id': 'a01', 'vote': 1, 'title': 'HACK', 'score': 99})
        self.assertEqual((st, body['vote'], body['changed']), (200, 1, True))
        self.assertEqual(preferences.feedback()['a01']['title'], 'Story 1')
        st, body, _ = self.req('POST', '/feedback', {'id': 'a01', 'vote': 1})
        self.assertFalse(body['changed'])
        self.assertEqual(self.req('GET', '/feedback')[1]['votes'], {'a01': 1})
        # undo works even after the article left the digest
        with open(os.environ['DIGEST_FILE'], 'w') as fh:
            json.dump({'articles': []}, fh)
        st, body, _ = self.req('POST', '/feedback', {'id': 'a01', 'vote': None})
        self.assertEqual((st, body['vote']), (200, 0))
        self.assertEqual(self.req('GET', '/feedback')[1]['votes'], {})

    def test_vote_does_not_mark_read_or_saved(self):
        self.req('PUT', '/state', {'readIds': [], 'later': [], 'history': []})
        self.req('POST', '/feedback', {'id': 'a02', 'vote': -1})
        st, state, _ = self.req('GET', '/state')
        self.assertEqual(state['readIds'], [])
        self.assertEqual(state['later'], [])

    # ── articles / discover ───────────────────────────────────────
    def test_articles_paging_never_repeats_and_hides_read(self):
        self.req('PUT', '/state', {'readIds': ['a00', 'a05'], 'later': [{'id': 'a06', 'title': 't'}]})
        seen, offset = [], 0
        while offset is not None:
            st, page, _ = self.req('GET', f'/articles?offset={offset}&limit=7')
            seen += [a['id'] for a in page['articles']]
            offset = page['next_offset']
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), 27)
        self.assertFalse({'a00', 'a05', 'a06'} & set(seen))
        st, page, _ = self.req('GET', '/articles?limit=5')
        for a in page['articles']:
            self.assertIn('fallback_image', a)
            self.assertIn('vote', a)
        self.assertEqual(self.req('GET', '/articles?offset=x')[0], 400)

    def test_articles_category_filter(self):
        st, page, _ = self.req('GET', '/articles?category=gaming&limit=100')
        self.assertTrue(page['articles'])
        self.assertTrue(all(a['category'] == 'gaming' for a in page['articles']))

    # ── refresh concurrency ───────────────────────────────────────
    def test_refresh_statuses(self):
        with mock.patch.object(pipeline, 'refresh_async', return_value='running'):
            self.assertEqual(self.req('POST', '/refresh')[0], 409)
        with mock.patch.object(pipeline, 'refresh_async', return_value='cooldown'):
            self.assertEqual(self.req('POST', '/refresh')[0], 429)
        with mock.patch.object(pipeline, 'refresh_async', return_value='started'):
            self.assertEqual(self.req('POST', '/refresh')[0], 202)

    # ── onboarding flow ───────────────────────────────────────────
    def test_onboarding_flow(self):
        st, ob, _ = self.req('GET', '/onboarding?n=5')
        self.assertEqual(st, 200)
        self.assertEqual(ob['onboarding']['status'], 'not_started')
        self.assertEqual(len(ob['candidates']), 5)
        for c in ob['candidates']:
            self.assertTrue(c['fallback_image'].startswith('/static/fallback/'))
        self.assertEqual(self.req('POST', '/onboarding/complete')[0], 400)
        ids = [a['id'] for a in digest_articles()][:10]
        for aid in ids:
            self.assertEqual(self.req('POST', '/feedback', {'id': aid, 'vote': 1, 'context': 'onboarding'})[0], 200)
        self.assertEqual(preferences.onboarding()['status'], 'in_progress')
        st, ob2, _ = self.req('GET', '/onboarding?n=20&exclude=' + ','.join(c['id'] for c in ob['candidates']))
        self.assertFalse(set(ids) & {c['id'] for c in ob2['candidates']})
        st, img, _ = self.req('GET', '/image?id=a11')
        self.assertTrue(img['fallback_image'])
        self.assertEqual(self.req('GET', '/image?id=nope')[0], 404)
        self.assertEqual(self.req('POST', '/onboarding/skip', {'id': 'a12'})[0], 200)
        with mock.patch.object(pipeline, 'refresh_async', return_value='started'):
            st, done, _ = self.req('POST', '/onboarding/complete')
        self.assertEqual((st, done['onboarding']['status']), (200, 'completed'))
        self.assertEqual(self.req('POST', '/onboarding/status', {'status': 'in_progress'})[0], 200)
        self.assertEqual(self.req('POST', '/onboarding/status', {'status': 'bogus'})[0], 400)

    def test_migration_for_existing_user(self):
        with open(os.environ['STATE_FILE'], 'w') as fh:
            json.dump({'history': [{'id': 'x'}]}, fh)
        server.migrate_preferences()
        self.assertEqual(preferences.onboarding()['status'], 'skipped')


if __name__ == '__main__':
    unittest.main()
