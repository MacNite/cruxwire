import http.server
import json
import math
import os
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import _env
import pipeline
import preferences
import settings

TOPICS = ['solar', 'nasa', 'election', 'football', 'earnings', 'gadget']


def fake_vec(text):
    """Deterministic 'embedding': one axis per topic word + a shared bias,
    so same-topic texts are ~0.9 similar and different topics ~0.3."""
    t = text.lower()
    v = [1.0 if w in t else 0.0 for w in TOPICS] + [0.45]
    if not any(v[:-1]):
        v[-1] = 1.0
    return v


def art(i, topic, score=6.0, source=None, category=None):
    title = f'{topic} story {i}'
    return {'id': f'{topic}{i}', 'title': title, 'url': f'https://ex.ex/{topic}/{i}',
            'source': source or f'src-{topic}', 'category': category or topic, 'score': score,
            'embedding': fake_vec(title)}


def ex(title):
    return {'title': title, 'source': 'x', 'vec': pipeline._normalize(fake_vec(title))}


class FeedbackRankingTests(unittest.TestCase):
    def setUp(self):
        _env.reset()
        # Calibrate to the fake space: unrelated ≈ 0.17, same topic ≈ 1.0
        self.p = mock.patch.multiple(pipeline, FEEDBACK_SIM_LO=0.3, FEEDBACK_SIM_HI=0.9)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_like_boosts_similar_dislike_demotes(self):
        arts = [art(1, 'solar'), art(2, 'football'), art(3, 'nasa')]
        pipeline.apply_feedback(arts, [ex('solar liked')], [ex('football hated')], 1.5, 1.0, 2.0)
        by = {a['id']: a for a in arts}
        self.assertGreater(by['solar1']['feedback_boost'], 1.0)
        self.assertLess(by['football2']['feedback_boost'], -0.5)
        self.assertAlmostEqual(by['nasa3']['feedback_boost'], 0.0, places=3)
        self.assertEqual(by['solar1'].get('why'), 'solar liked')
        self.assertTrue(by['nasa3']['explore'])
        self.assertFalse(by['solar1']['explore'])

    def test_multiple_interests_all_survive(self):
        arts = [art(1, 'solar'), art(2, 'nasa'), art(3, 'earnings')]
        pos = [ex('solar a'), ex('solar b'), ex('solar c'), ex('nasa a')]
        pipeline.apply_feedback(arts, pos, [], 1.5, 1.0, 2.0)
        by = {a['id']: a['feedback_boost'] for a in arts}
        # A centroid would wash out the single NASA like; top-k doesn't.
        self.assertGreater(by['nasa2'], 0.5)
        self.assertGreater(by['solar1'], 0.5)
        self.assertEqual(by['earnings3'], 0.0)

    def test_cap_limits_boost_and_penalty(self):
        arts = [art(1, 'solar'), art(2, 'football')]
        pipeline.apply_feedback(arts, [ex('solar')], [ex('football')], 10.0, 10.0, 1.25)
        self.assertEqual(arts[0]['feedback_boost'], 1.25)
        self.assertEqual(arts[1]['feedback_boost'], -1.25)

    def test_source_affinity_is_separate_and_small(self):
        for i in range(3):
            preferences.set_vote(f'a{i}', {'title': f'solar {i}', 'url': f'https://p.ex/{i}', 'source': 'Liked Pub'}, 1)
        aff = pipeline.source_affinity(0.5)
        self.assertGreater(aff['Liked Pub'], 0)
        self.assertLessEqual(aff['Liked Pub'], pipeline.SOURCE_AFFINITY_CAP)
        one = pipeline.source_affinity(0.5)
        preferences.set_vote('b', {'title': 'x', 'url': 'https://q.ex/1', 'source': 'Once'}, 1)
        self.assertLess(pipeline.source_affinity(0.5)['Once'], one['Liked Pub'])
        # topic boost on an unrelated story from the liked publisher stays 0
        a = [art(1, 'football', source='Liked Pub')]
        pipeline.apply_feedback(a, [ex('solar')], [], 1.5, 1.0, 2.0, affinity=aff)
        self.assertEqual(a[0]['feedback_boost'], 0.0)
        self.assertGreater(a[0]['source_boost'], 0.0)

    def test_votes_change_order_vs_baseline(self):
        arts = [art(1, 'football', 7.0), art(2, 'solar', 6.0), art(3, 'nasa', 6.5)]
        base = [a['id'] for a in sorted(arts, key=pipeline.rank_score, reverse=True)]
        pipeline.apply_feedback(arts, [ex('solar')], [ex('football')], 1.5, 1.0, 2.0)
        after = [a['id'] for a in sorted(arts, key=pipeline.rank_score, reverse=True)]
        self.assertEqual(base[0], 'football1')
        self.assertEqual(after[0], 'solar2')
        self.assertEqual(after[-1], 'football1')


class DiversifyTests(unittest.TestCase):
    def test_single_topic_cannot_dominate_top10(self):
        items = [dict(art(i, 'solar', 9.0 - i * 0.01), source=f's{i % 4}') for i in range(20)]
        items += [art(i, t, 5.0) for i, t in enumerate(['nasa', 'election', 'earnings', 'gadget', 'football', 'nasa'])]
        out = pipeline.diversify(items, explore_share=0)
        top = [a['category'] for a in out[:10]]
        self.assertLessEqual(top.count('solar'), pipeline.MAX_PER_TOPIC_TOP)
        self.assertEqual(len(out), len(items))
        self.assertEqual(out[0]['category'], 'solar')

    def test_exploration_share(self):
        items = [dict(art(i, 'solar', 9.0), source=f's{i}', category=f'c{i % 5}') for i in range(12)]
        items += [dict(art(i, 'nasa', 3.0), source=f'x{i}', category=f'e{i}', explore=True) for i in range(6)]
        out = pipeline.diversify(items, explore_share=0.25)
        top8 = out[:8]
        self.assertEqual(sum(1 for a in top8 if a.get('explore')), 2)
        self.assertTrue(out[3].get('explore'))
        self.assertFalse(any(a.get('explore') for a in out[:3]))

    def test_no_explore_candidates_is_fine(self):
        items = [art(i, 'solar') for i in range(5)]
        self.assertEqual(len(pipeline.diversify(items, explore_share=0.25)), 5)


class BudgetTests(unittest.TestCase):
    def test_round_robin_budget(self):
        arts = [{'id': f'a{i}', 'source': 'big', 'published_at': f'2026-11-13T{i:02d}:00:00+00:00'} for i in range(20)]
        arts += [{'id': f'b{i}', 'source': 'small', 'published_at': f'2026-11-12T{i:02d}:00:00+00:00'} for i in range(3)]
        picked, deferred = pipeline.select_new_for_scoring(arts, 6)
        self.assertEqual(len(picked), 6)
        self.assertEqual(deferred, 17)
        self.assertEqual(sum(1 for a in picked if a['source'] == 'small'), 3)
        self.assertEqual(pipeline.select_new_for_scoring(arts[:3], 40), (arts[:3], 0))


# ── End-to-end run against a fake Ollama ─────────────────────────────
class _FakeOllama(http.server.BaseHTTPRequestHandler):
    inflight = 0
    peak = 0
    chat_calls = 0
    embed_calls = 0
    lock = threading.Lock()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        cls = type(self)
        with cls.lock:
            cls.inflight += 1
            cls.peak = max(cls.peak, cls.inflight)
        time.sleep(0.01)
        try:
            if self.path == '/api/chat':
                with cls.lock:
                    cls.chat_calls += 1
                content = json.dumps({'score': 6.0, 'summary': 'fake summary', 'category': 'tech'})
                out = {'message': {'content': content}}
            else:
                with cls.lock:
                    cls.embed_calls += 1
                out = {'embedding': fake_vec(body.get('prompt', ''))}
        finally:
            with cls.lock:
                cls.inflight -= 1
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"models": []}')

    def log_message(self, *a):
        pass


def feed_xml(source, n, offset_h=0):
    now = datetime.now(timezone.utc)
    items = ''.join(
        f'<item><title>{source} {TOPICS[i % 5]} item {i}</title><link>https://{source}.ex/{i}</link>'
        f'<pubDate>{(now - timedelta(hours=i + offset_h)).strftime("%a, %d %b %Y %H:%M:%S GMT")}</pubDate>'
        f'<description>about {TOPICS[i % 5]}</description></item>' for i in range(n))
    return f'<rss><channel>{items}</channel></rss>'


class PipelineRunTests(unittest.TestCase):
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
        _FakeOllama.peak = _FakeOllama.chat_calls = _FakeOllama.embed_calls = 0
        feeds = [{'url': 'https://a.ex/rss', 'source': 'a', 'category': 'tech'},
                 {'url': 'https://b.ex/rss', 'source': 'b', 'category': 'tech'}]
        with open(os.environ['FEEDS_FILE'], 'w') as fh:
            json.dump(feeds, fh)
        settings.save({'max_new_scored': 8, 'score_concurrency': 4, 'retain_floor': 0,
                       'retain_ceiling': 200})
        bodies = {'https://a.ex/rss': feed_xml('a', 10), 'https://b.ex/rss': feed_xml('b', 10)}
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

    def _digest(self):
        with open(os.environ['DIGEST_FILE']) as fh:
            return json.load(fh)

    def test_budget_reuse_single_job_and_votes(self):
        self.assertTrue(pipeline.run_once())
        self.assertEqual(_FakeOllama.chat_calls, 8, 'only max_new_scored new articles are scored')
        self.assertEqual(_FakeOllama.peak, 1, 'one model job at a time, even with 4 workers')
        d = self._digest()
        self.assertEqual(d['article_count'], 8)
        for a in d['articles']:
            self.assertTrue(a['fallback_image'].startswith('/static/fallback/'))
            self.assertNotIn('embedding', a)
        run = pipeline.get_runs()[-1]
        self.assertEqual(run['deferred_count'], 12)
        self.assertIsNotNone(run['avg_enrich_ms'])

        # Second run: the 8 scored articles are reused (no chat calls for them);
        # the budget goes to 8 of the 12 deferred ones.
        _FakeOllama.chat_calls = 0
        self.assertTrue(pipeline.run_once())
        self.assertEqual(_FakeOllama.chat_calls, 8)
        self.assertEqual(pipeline.get_runs()[-1]['reused_count'], 8)
        self.assertEqual(self._digest()['article_count'], 16)

        # Explicit votes move rankings on the next run.
        d = self._digest()['articles']
        solar = next(a for a in d if 'solar' in a['title'])
        nasa = next(a for a in d if 'nasa' in a['title'])
        preferences.set_vote(solar['id'], solar, 1)
        preferences.set_vote(nasa['id'], nasa, -1)
        with mock.patch.multiple(pipeline, FEEDBACK_SIM_LO=0.3, FEEDBACK_SIM_HI=0.9):
            pipeline.run_once()
        d = self._digest()['articles']
        top = d[0]
        self.assertIn('solar', top['title'])
        nasa_boosts = [a['feedback_boost'] for a in d if 'nasa' in a['title']]
        self.assertTrue(all(b < 0 for b in nasa_boosts))

    def test_ollama_down_degrades_gracefully(self):
        with mock.patch.object(pipeline, 'OLLAMA_HOST', 'http://127.0.0.1:9'):
            self.assertTrue(pipeline.run_once())
        d = self._digest()
        self.assertEqual(d['article_count'], 8)
        self.assertEqual(pipeline.get_runs()[-1]['score_fallbacks'], 8)

    def test_refresh_never_overlaps(self):
        with pipeline._run_lock:
            self.assertEqual(pipeline.refresh_async(force=True), 'running')
        pipeline._last_manual_refresh = 0.0
        self.assertEqual(pipeline.refresh_async(), 'started')
        self.assertIn(pipeline.refresh_async(), ('running', 'cooldown'))
        for _ in range(200):
            if not pipeline.get_status()['running']:
                break
            time.sleep(0.05)
        self.assertEqual(pipeline.refresh_async(), 'cooldown')


if __name__ == '__main__':
    unittest.main()
