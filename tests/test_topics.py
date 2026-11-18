"""Multi-topic categories: config fields, the scorer's topics, topic
assignment, re-filing after a category edit, and the API / state filters."""
import http.client
import json
import os
import threading
import unittest
from unittest import mock

import _env
import pipeline
import server
import settings

# Tiny fixed "embedding space": one axis per category, so similarities are exact.
AXES = ['ai', 'hardware', 'politics', 'games', 'security']


def vec(*weights):
    return [float(w) for w in weights]


CAT_VECS = [(k, [1.0 if i == j else 0.0 for j in range(len(AXES))]) for i, k in enumerate(AXES)]


def cats(*keys, **overrides):
    out = []
    for k in keys:
        c = {'key': k, 'label': k.title(), 'color': '#123456', 'interest': f'{k} news'}
        c.update(overrides.get(k, {}))
        out.append(c)
    return out


class CategoryConfigTests(unittest.TestCase):
    def setUp(self):
        _env.reset()

    def test_group_and_boost_are_coerced(self):
        saved = settings.save_categories([
            {'key': 'ai', 'label': 'AI', 'group': '  Tech   stuff ', 'boost': True},
            {'key': 'politics', 'label': 'Politics', 'boost': False},
            {'key': 'games', 'label': 'Games', 'boost': 'false'},
            {'key': 'hardware', 'label': 'HW'},
        ])
        by = {c['key']: c for c in saved}
        self.assertEqual(by['ai']['group'], 'Tech stuff')
        self.assertTrue(by['ai']['boost'])
        self.assertFalse(by['politics']['boost'])
        self.assertFalse(by['games']['boost'])
        self.assertTrue(by['hardware']['boost'], 'boost defaults to true')
        self.assertEqual(by['hardware']['group'], '')
        self.assertEqual(settings.load_categories(), saved)

    def test_boost_categories_never_empty(self):
        c = cats('ai', 'politics', politics={'boost': False})
        self.assertEqual([x['key'] for x in settings.boost_categories(c)], ['ai'])
        none = cats('ai', ai={'boost': False})
        self.assertEqual([x['key'] for x in settings.boost_categories(none)], ['ai'])

    def test_prompt_leaves_filter_only_categories_out_of_interests(self):
        c = cats('ai', 'politics', politics={'boost': False, 'interest': 'POLITICS-LINE'})
        prompt = pipeline.build_system_prompt(c)
        self.assertIn('ai news', prompt)
        self.assertNotIn('POLITICS-LINE', prompt)
        self.assertIn('topics', prompt)
        # The neutral (multi-user) prompt describes every topic.
        self.assertIn('POLITICS-LINE', pipeline.build_neutral_prompt(c))


class ScorerTopicTests(unittest.TestCase):
    def _score(self, content, valid=('ai', 'hardware', 'politics')):
        art = {'title': 't', 'source': 's', 'category': 'ai'}
        with mock.patch.object(pipeline, 'ollama_post',
                               return_value={'message': {'content': json.dumps(content)}}):
            return pipeline._score(art, 'x', 'm', 'sys', list(valid))

    def test_topics_are_cleaned_and_capped(self):
        _, _, topics, ok, _ = self._score(
            {'score': 7, 'summary': 's', 'topics': ['Hardware', 'nope', 'hardware', 'ai', 'politics', 'ai']})
        self.assertTrue(ok)
        self.assertEqual(topics, ['hardware', 'ai', 'politics'])

    def test_legacy_category_answer_still_works(self):
        _, _, topics, _, _ = self._score({'score': 7, 'summary': 's', 'category': 'politics'})
        self.assertEqual(topics, ['politics'])

    def test_no_valid_topic_returns_empty(self):
        _, _, topics, _, _ = self._score({'score': 7, 'summary': 's', 'topics': 'bogus'})
        self.assertEqual(topics, [])

    def test_enrich_records_topics_and_falls_back_to_feed_category(self):
        art = {'id': 'x', 'title': 't', 'url': 'https://e.x/1', 'source': 's', 'category': 'ai',
               'published_at': '2026-11-13T05:00:00+00:00'}
        with mock.patch.object(pipeline, '_fetch_page', return_value=(None, '')), \
             mock.patch.object(pipeline, '_embed', return_value=None), \
             mock.patch.object(pipeline, '_score', return_value=(6.0, 's', ['politics', 'ai'], True, 1.0)):
            out = pipeline.enrich(art, 'm', 'e', 'sys', ['ai', 'politics'])
        self.assertEqual((out['category'], out['topics']), ('politics', ['politics', 'ai']))
        self.assertEqual(out['llm_vocab'], ['ai', 'politics'])
        with mock.patch.object(pipeline, '_fetch_page', return_value=(None, '')), \
             mock.patch.object(pipeline, '_embed', return_value=None), \
             mock.patch.object(pipeline, '_score', return_value=(5.0, 's', [], False, 1.0)):
            out = pipeline.enrich(art, 'm', 'e', 'sys', ['ai', 'politics'])
        self.assertEqual((out['category'], out['topics'], out['llm_category']), ('ai', ['ai'], None))

    def test_shortlist_offers_closest_categories_plus_hint(self):
        valid = AXES
        sims = {'ai': 0.9, 'hardware': 0.8, 'politics': 0.1, 'games': 0.2, 'security': 0.3}
        self.assertEqual(pipeline.topic_candidates(sims, valid, k=10), valid, 'short list: offer all')
        self.assertEqual(pipeline.topic_candidates(sims, valid, hint='politics', k=2),
                         ['ai', 'hardware', 'politics'])
        self.assertEqual(pipeline.topic_candidates({}, valid, k=2), valid, 'no embedding: offer all')

    def test_enrich_uses_shortlist_with_many_categories(self):
        art = {'id': 'x', 'title': 't', 'url': 'https://e.x/1', 'source': 's', 'category': 'games',
               'published_at': '2026-11-13T05:00:00+00:00'}
        seen = {}

        def fake_score(article, excerpt, chat_model, system_prompt, valid_cats):
            seen['offered'] = valid_cats
            return 6.0, 's', ['ai'], True, 1.0
        with mock.patch.object(pipeline, 'TOPIC_CANDIDATES', 2), \
             mock.patch.object(pipeline, '_fetch_page', return_value=(None, '')), \
             mock.patch.object(pipeline, '_embed', return_value=vec(1, 0.5, 0, 0, 0)), \
             mock.patch.object(pipeline, '_score', side_effect=fake_score):
            out = pipeline.enrich(art, 'm', 'e', 'sys', AXES, cat_vecs=CAT_VECS)
        self.assertEqual(seen['offered'], ['ai', 'hardware', 'games'])
        self.assertEqual(out['llm_vocab'], ['ai', 'hardware', 'games'])


class AssignTopicsTests(unittest.TestCase):
    def test_scorer_picks_win_and_secondaries_kept(self):
        a = {'embedding': vec(1, 1, 0, 0, 0), 'llm_topics': ['hardware', 'ai'], 'llm_vocab': AXES}
        pipeline.assign_topics([a], cats(*AXES), CAT_VECS)
        self.assertEqual(a['category'], 'hardware')
        self.assertEqual(a['topics'], ['hardware', 'ai'])

    def test_unknown_picks_fall_back_to_feed_hint_then_embedding(self):
        a = {'embedding': vec(0, 0, 1, 0, 0), 'llm_topics': ['gone'], 'llm_vocab': ['gone'],
             '_hint_cat': 'games'}
        b = {'embedding': vec(0, 0, 1, 0, 0), 'llm_topics': [], 'llm_vocab': ['ai']}
        pipeline.assign_topics([a, b], cats(*AXES), CAT_VECS)
        self.assertEqual(a['category'], 'games')
        self.assertEqual(b['category'], 'politics')

    def test_new_category_attached_by_embedding_only_if_not_offered(self):
        # Scored when only ai/hardware existed; security was added later.
        a = {'embedding': vec(1, 0, 0, 0, 1), 'llm_topics': ['ai'], 'llm_vocab': ['ai', 'hardware']}
        # Here the scorer WAS offered security and chose not to tag it.
        b = {'embedding': vec(1, 0, 0, 0, 1), 'llm_topics': ['ai'], 'llm_vocab': AXES}
        pipeline.assign_topics([a, b], cats(*AXES), CAT_VECS)
        self.assertEqual(a['topics'], ['ai', 'security'])
        self.assertEqual(b['topics'], ['ai'])

    def test_legacy_record_without_topics(self):
        a = {'embedding': None, 'category': 'politics', 'llm_category': 'politics'}
        pipeline.assign_topics([a], cats(*AXES), CAT_VECS)
        self.assertEqual((a['category'], a['topics']), ('politics', ['politics']))

    def test_topics_capped(self):
        a = {'embedding': vec(1, 1, 1, 1, 1), 'llm_topics': AXES, 'llm_vocab': AXES}
        pipeline.assign_topics([a], cats(*AXES), CAT_VECS)
        self.assertEqual(len(a['topics']), pipeline.MAX_TOPICS)

    def test_interest_ignores_filter_only_categories(self):
        c = cats('ai', 'politics', politics={'boost': False})
        a = {'embedding': vec(0, 0, 1, 0, 0), 'score': 5.0}
        pipeline.apply_interest([a], c, CAT_VECS, 1.0)
        self.assertEqual(a['interest_match'], 0.0, 'a pure-politics story gets no interest boost')


class RetagTests(unittest.TestCase):
    def setUp(self):
        _env.reset()

    def test_retag_refiles_stored_digest(self):
        settings.save_categories(cats('ai', 'security'))
        embed_model = settings.load()['embed_model']
        with open(os.environ['DIGEST_FILE'], 'w') as fh:
            json.dump({'articles': [{'id': 'x', 'title': 't', 'category': 'ai', 'topics': ['ai'],
                                     'llm_topics': ['ai'], 'llm_vocab': ['ai']}]}, fh)
        with open(os.environ['EMBEDDINGS_FILE'], 'w') as fh:
            json.dump({'model': embed_model, 'vectors': {'x': [1.0, 1.0]}}, fh)

        def fake_embed(text, model=None):
            return [1.0, 0.0] if text.startswith('Ai') else [0.0, 1.0]
        with mock.patch.object(pipeline, '_embed', side_effect=fake_embed):
            self.assertTrue(pipeline.retag_digest())
        with open(os.environ['DIGEST_FILE']) as fh:
            art = json.load(fh)['articles'][0]
        self.assertEqual(art['topics'], ['ai', 'security'])
        self.assertNotIn('embedding', art)

    def test_retag_skips_while_a_run_is_active(self):
        with pipeline._run_lock:
            self.assertFalse(pipeline.retag_digest())


def topic_digest():
    rows = [('a1', 'ai', ['ai', 'hardware', 'politics']), ('a2', 'ai', ['ai']),
            ('a3', 'politics', ['politics']), ('a4', 'hardware', ['hardware', 'ai']),
            ('a5', 'games', None)]
    return [{'id': i, 'title': i, 'url': f'https://e.x/{i}', 'source': 's', 'category': c,
             **({'topics': t} if t else {}), 'score': 5, 'summary': '',
             'published_at': '2026-11-13T05:00:00+00:00', 'cluster_id': i, 'cluster_rep': True}
            for i, c, t in rows]


class TopicApiTests(unittest.TestCase):
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
            json.dump({'articles': topic_digest()}, fh)

    def _get(self, path):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        c.request('GET', path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, json.loads(body or b'null')

    def ids(self, path):
        status, body = self._get(path)
        self.assertEqual(status, 200)
        return sorted(a['id'] for a in body['articles'])

    def test_filters(self):
        self.assertEqual(self.ids('/articles?category=hardware'), ['a1', 'a4'],
                         'the legacy param matches secondary topics too')
        self.assertEqual(self.ids('/articles?topics=politics,games'), ['a1', 'a3', 'a5'])
        self.assertEqual(self.ids('/articles?topics=ai,hardware&mode=all'), ['a1', 'a4'])
        self.assertEqual(self.ids('/articles?hide=politics'), ['a2', 'a4', 'a5'])
        self.assertEqual(self.ids('/articles?topics=ai&hide=hardware'), ['a2'])
        self.assertEqual(self._get('/articles?mode=some')[0], 400)

    def test_saved_items_and_hidden_topics_in_state(self):
        ok = {'id': 'x', 'topics': ['ai', 'politics']}
        self.assertTrue(server._valid_saved_item(ok))
        self.assertFalse(server._valid_saved_item({'id': 'x', 'topics': 'ai'}))
        self.assertFalse(server._valid_saved_item({'id': 'x', 'topics': [1]}))
        st = server.normalize_payload({'viewState': {'hiddenCats': ['politics', 3, 'x' * 99]}})
        self.assertEqual(st['viewState']['hiddenCats'], ['politics', 'x' * 40])
        st = server.normalize_payload({'viewState': {'hiddenCats': 'politics'}})
        self.assertEqual(st['viewState']['hiddenCats'], [])

    def test_saving_categories_triggers_refile(self):
        with mock.patch.object(pipeline, 'retag_digest_async') as kick:
            c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
            c.request('PUT', '/categories', body=json.dumps(cats('ai')),
                      headers={'Content-Type': 'application/json'})
            r = c.getresponse()
            r.read()
            c.close()
        self.assertEqual(r.status, 200)
        kick.assert_called_once()


if __name__ == '__main__':
    unittest.main()
