import json
import os
import random
import unittest
from datetime import datetime, timedelta, timezone

import _env
import onboarding
import preferences


def rss(source, n, topic_words):
    now = datetime.now(timezone.utc)
    items = []
    for i in range(n):
        pub = (now - timedelta(hours=i)).strftime('%a, %d %b %Y %H:%M:%S GMT')
        w = topic_words[i % len(topic_words)]
        img = f'<enclosure url="https://{source}.ex/{i}.jpg" type="image/jpeg"/>' if i % 2 == 0 else ''
        items.append(f'<item><title>{source} {w} story {i}</title><link>https://{source}.ex/a/{i}</link>'
                     f'<pubDate>{pub}</pubDate><description>About {w}</description>{img}</item>')
    return f'<rss><channel><title>{source}</title>{"".join(items)}</channel></rss>'


FEEDS = [{'url': 'https://big.ex/rss', 'source': 'big', 'category': 'tech'},
         {'url': 'https://small.ex/rss', 'source': 'small', 'category': 'tech'},
         {'url': 'https://down.ex/rss', 'source': 'down', 'category': 'tech'}]
BODIES = {'https://big.ex/rss': rss('big', 30, ['solar', 'nasa', 'election', 'football']),
          'https://small.ex/rss': rss('small', 4, ['earnings']),
          'https://down.ex/rss': ''}


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        _env.reset()
        with open(os.environ['FEEDS_FILE'], 'w') as fh:
            json.dump(FEEDS, fh)
        self.pool = onboarding.refresh_pool(FEEDS, fetch=lambda u: BODIES.get(u, ''))

    def test_pool_reports_feed_errors_and_has_fallbacks(self):
        self.assertEqual([e['source'] for e in self.pool['feedErrors']], ['down'])
        self.assertEqual(len(self.pool['articles']), 34)
        for a in self.pool['articles']:
            # The image guarantee: every candidate has a local fallback.
            self.assertTrue(a['fallback_image'].startswith('/static/fallback/'))
            self.assertTrue(a['title'] and a['url'] and a['publisher'])

    def test_batch_rotates_feeds_and_topics(self):
        batch, remaining = onboarding.select_batch(self.pool['articles'], {}, set(), n=8,
                                                   rng=random.Random(1))
        self.assertEqual(len(batch), 8)
        srcs = [a['source'] for a in batch]
        self.assertGreaterEqual(srcs.count('small'), 3, 'small feed must not be drowned out')
        big_topics = {a['topic'] for a in batch if a['source'] == 'big'}
        self.assertGreaterEqual(len(big_topics), 3, 'topics should rotate within a feed')
        self.assertEqual(remaining, 34 - 8)

    def test_voted_skipped_and_excluded_never_return(self):
        arts = self.pool['articles']
        preferences.set_vote(arts[0]['id'], arts[0], 1)
        preferences.add_skip(arts[1]['id'])
        fb = preferences.feedback()
        batch, _ = onboarding.select_batch(arts, fb, preferences.skipped_ids(),
                                           exclude=[arts[2]['id']], n=40)
        ids = {a['id'] for a in batch}
        self.assertFalse(ids & {arts[0]['id'], arts[1]['id'], arts[2]['id']})

    def test_exhausted_inventory(self):
        arts = self.pool['articles']
        for a in arts:
            preferences.add_skip(a['id'])
        out = onboarding.status_payload(n=8)
        self.assertTrue(out['exhausted'])
        self.assertEqual(out['candidates'], [])

    def test_progress_and_completion_threshold(self):
        arts = self.pool['articles']
        for a in arts[:9]:
            preferences.set_vote(a['id'], a, 1)
        ok, out = onboarding.complete(trigger_refresh=False)
        self.assertFalse(ok)
        self.assertIn('9/10', out['error'])
        preferences.set_vote(arts[9]['id'], arts[9], 1)
        ok, out = onboarding.complete(trigger_refresh=False)
        self.assertTrue(ok)
        self.assertEqual(preferences.onboarding()['status'], 'completed')
        # Re-opening the wizard keeps the votes.
        preferences.set_status('in_progress')
        self.assertEqual(onboarding.progress_payload()['likes'], 10)

    def test_find_article_server_side_only(self):
        a = self.pool['articles'][0]
        found, origin = onboarding.find_article(a['id'])
        self.assertEqual(found['url'], a['url'])
        self.assertEqual(origin, 'onboarding')
        self.assertEqual(onboarding.find_article('nope'), (None, None))
        self.assertEqual(onboarding.find_article('x' * 100), (None, None))

    def test_resolve_image_always_returns_fallback(self):
        a = next(x for x in self.pool['articles'] if not x['image'])
        r = onboarding.resolve_image(a['id'], fetcher=lambda u: (_ for _ in ()).throw(TimeoutError()))
        self.assertIsNone(r['image'])
        self.assertTrue(r['fallback_image'].endswith('.svg'))

    def test_seed_import(self):
        page = ('<html><head><title>T</title><meta property="og:title" content="Seeded story">'
                '<meta name="description" content="About seeds"><meta property="og:site_name" content="Seed Times">'
                '<link rel="canonical" href="https://seed.ex/story"></head></html>')
        res = onboarding.import_seeds(['https://seed.ex/story?utm_source=x', 'https://empty.ex/'],
                                      fetcher=lambda u: (u, page if 'seed' in u else '<html></html>'))
        self.assertTrue(res[0]['ok'])
        self.assertFalse(res[1]['ok'])
        e = preferences.feedback()[res[0]['id']]
        self.assertEqual((e['title'], e['source'], e['origin'], e['vote']), ('Seeded story', 'Seed Times', 'seed', 1))

    def test_seed_private_url_blocked(self):
        res = onboarding.import_seeds(['http://192.168.1.1/admin'])
        self.assertFalse(res[0]['ok'])
        self.assertIn('Blocked', res[0]['error'])

    def test_google_news_search_url(self):
        u = onboarding.google_news_search_url('heat pumps', 'de', 'DE')
        self.assertTrue(u.startswith('https://news.google.com/rss/search?q=heat+pumps'))
        self.assertIn('ceid=DE%3Ade', u)


if __name__ == '__main__':
    unittest.main()
