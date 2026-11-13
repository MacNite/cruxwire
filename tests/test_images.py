import base64
import os
import unittest

import _env
import images
import pipeline


class ImageExtractionTests(unittest.TestCase):
    def setUp(self):
        _env.reset()

    def test_media_content_prefers_largest(self):
        block = ('<title>x</title><media:thumbnail url="https://cdn.ex/small.jpg" width="150"/>'
                 '<media:content url="https://cdn.ex/big.jpg" medium="image" width="1200"/>')
        self.assertEqual(images.extract_rss_image(block), 'https://cdn.ex/big.jpg')

    def test_media_video_is_ignored_and_enclosure_used(self):
        block = ('<media:content url="https://cdn.ex/v.mp4" medium="video"/>'
                 '<enclosure url="https://cdn.ex/e.png" type="image/png" length="1"/>')
        self.assertEqual(images.extract_rss_image(block), 'https://cdn.ex/e.png')

    def test_escaped_img_in_description_with_relative_url(self):
        block = ('<description>&lt;p&gt;&lt;img src="/img/a.jpg" width="600"&gt;text&lt;/p&gt;</description>')
        self.assertEqual(images.extract_rss_image(block, base='https://pub.example/story/1'),
                         'https://pub.example/img/a.jpg')

    def test_tracking_pixel_skipped(self):
        block = '<description><![CDATA[<img src="https://t.ex/p.gif" width="1" height="1">]]></description>'
        self.assertIsNone(images.extract_rss_image(block))

    def test_missing_media_returns_none(self):
        self.assertIsNone(images.extract_rss_image('<title>No pics</title><description>plain</description>'))

    def test_og_image_relative_and_twitter(self):
        html = '<head><meta content="/a/b.jpg" property="og:image"></head>'
        self.assertEqual(images.extract_page_image(html, 'https://news.example/x/y'),
                         'https://news.example/a/b.jpg')
        html2 = "<meta name='twitter:image' content='//cdn.example/t.webp'>"
        self.assertEqual(images.extract_page_image(html2, 'https://news.example/'),
                         'https://cdn.example/t.webp')

    def test_normalize_rejects_unsafe(self):
        for bad in ['javascript:alert(1)', 'data:image/png;base64,AAAA', '', '   ', 'ftp://x/y.jpg']:
            self.assertIsNone(images.normalize_image_url(bad), bad)
        self.assertEqual(images.normalize_image_url(' https://a.ex/i.jpg?x=1&amp;y=2#frag '),
                         'https://a.ex/i.jpg?x=1&y=2')

    def test_canonical_url_dedupes_tracking(self):
        a = images.canonical_url('HTTPS://Pub.Example/story/?utm_source=rss&id=3#top')
        b = images.canonical_url('https://pub.example/story?id=3')
        self.assertEqual(a, b)

    def test_google_news_decode(self):
        payload = b'\x08\x13"\x2ahttps://publisher.example/2026/11/story.html\xd2\x01\x00'
        token = base64.urlsafe_b64encode(payload).decode().rstrip('=')
        url = f'https://news.google.com/rss/articles/{token}?oc=5'
        self.assertEqual(images.decode_google_news_url(url), 'https://publisher.example/2026/11/story.html')
        # undecodable (new-style) ids degrade to None → fallback, never raise
        self.assertIsNone(images.decode_google_news_url('https://news.google.com/rss/articles/CBMiAAAA'))

    def test_every_topic_has_a_fallback_file(self):
        for t in images.FALLBACK_TOPICS:
            path = os.path.join(_env.ROOT, images.fallback_image(t).lstrip('/'))
            self.assertTrue(os.path.isfile(path), path)
        self.assertEqual(images.fallback_image('nonsense'), '/static/fallback/general.svg')

    def test_topic_inference(self):
        self.assertEqual(images.infer_topic('New solar battery plant opens'), 'energy')
        self.assertEqual(images.infer_topic('NASA telescope spots planet'), 'science')
        self.assertEqual(images.infer_topic('Quarterly earnings beat', category=''), 'business')
        self.assertEqual(images.infer_topic('Untitled', category='gaming'), 'gaming')
        self.assertEqual(images.infer_topic('Untitled'), 'general')


class ImageResolutionTests(unittest.TestCase):
    """Resolution order + the guarantee: an image OR a local fallback, always."""

    def setUp(self):
        _env.reset()

    def test_rss_image_wins_without_fetch(self):
        called = []
        r = images.resolve_for_article({'url': 'https://p.ex/1', 'title': 'Tech', 'image': 'https://c.ex/i.jpg'},
                                       fetcher=lambda u: called.append(u))
        self.assertEqual(r['image'], 'https://c.ex/i.jpg')
        self.assertEqual(r['image_kind'], 'rss')
        self.assertFalse(called)

    def test_og_fallback_then_cache(self):
        calls = []

        def fetcher(u):
            calls.append(u)
            return u, '<meta property="og:image" content="/hero.jpg">'
        a = {'url': 'https://p.ex/story', 'title': 'Battery news'}
        r = images.resolve_for_article(a, fetcher=fetcher)
        self.assertEqual(r['image'], 'https://p.ex/hero.jpg')
        images.resolve_for_article(a, fetcher=fetcher)
        self.assertEqual(len(calls), 1, 'second lookup must hit the cache')

    def test_failures_degrade_to_fallback(self):
        def boom(u):
            raise TimeoutError('slow site')
        for fetcher in (boom, lambda u: (u, '<html>no meta</html>'), lambda u: (u, '')):
            _env.reset()
            r = images.resolve_for_article({'url': 'https://slow.ex/a', 'title': 'Solar farm'}, fetcher=fetcher)
            self.assertIsNone(r['image'])
            self.assertEqual(r['fallback_image'], '/static/fallback/energy.svg')

    def test_blocked_private_page_degrades(self):
        r = images.resolve_for_article({'url': 'http://127.0.0.1/a', 'title': 'x'})
        self.assertIsNone(r['image'])
        self.assertTrue(r['fallback_image'].startswith('/static/fallback/'))

    def test_google_news_interstitial_is_not_a_preview(self):
        r = images.resolve_for_article(
            {'url': 'https://news.google.com/rss/articles/CBMiXYZ', 'title': 'Grid upgrade'},
            fetcher=lambda u: ('https://news.google.com/articles/CBMiXYZ',
                               '<meta property="og:image" content="https://news.google.com/logo.png">'))
        self.assertIsNone(r['image'])
        self.assertEqual(r['fallback_image'], '/static/fallback/energy.svg')


class FeedParsingTests(unittest.TestCase):
    RSS = '''<?xml version="1.0"?><rss xmlns:media="http://search.yahoo.com/mrss/"><channel><title>T</title>
      <item><title>Wind &amp; solar record</title><link>https://pub.ex/a</link>
        <pubDate>Fri, 13 Nov 2026 05:00:00 GMT</pubDate>
        <description><![CDATA[<p>Big <b>news</b> &amp; more</p><script>evil()</script>]]></description>
        <media:content url="https://pub.ex/a.jpg" medium="image"/></item>
      <item rdf:about="x"><title>Second</title><link>https://pub.ex/b</link>
        <pubDate>Fri, 13 Nov 2026 04:00:00 GMT</pubDate></item>
    </channel></rss>'''

    GNEWS = '''<rss><channel><item><title>Grid gets upgrade - Example Times</title>
      <link>https://news.google.com/rss/articles/CBMiXYZ?oc=5</link>
      <pubDate>Fri, 13 Nov 2026 05:00:00 GMT</pubDate>
      <description>&lt;a href="x"&gt;Grid gets upgrade&lt;/a&gt; Example Times</description>
      <source url="https://example-times.ex">Example Times</source></item></channel></rss>'''

    def test_parse_description_image_sanitised(self):
        from datetime import datetime, timezone
        items = pipeline.parse_feed(self.RSS, 'Feed', 'tech', datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(len(items), 2)
        a = items[0]
        self.assertEqual(a['title'], 'Wind & solar record')
        self.assertEqual(a['description'], 'Big news & more')
        self.assertNotIn('evil', a['description'])
        self.assertEqual(a['image'], 'https://pub.ex/a.jpg')
        self.assertIsNone(items[1]['image'])

    def test_google_news_publisher(self):
        from datetime import datetime, timezone
        items = pipeline.parse_feed(self.GNEWS, 'GN: grid', 'tech', datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(items[0]['title'], 'Grid gets upgrade')
        self.assertEqual(items[0]['publisher'], 'Example Times')
        self.assertEqual(items[0]['source'], 'GN: grid')


if __name__ == '__main__':
    unittest.main()
