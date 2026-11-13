import json
import os
import unittest

import _env
import preferences


def art(i, **kw):
    a = {'id': f'id{i}', 'title': f'Story {i}', 'url': f'https://pub{i % 3}.ex/s/{i}',
         'description': f'Desc {i}', 'source': f'Pub{i % 3}', 'category': 'tech'}
    a.update(kw)
    return a


class VotingTests(unittest.TestCase):
    def setUp(self):
        _env.reset()

    def test_like_is_idempotent(self):
        e1, ch1 = preferences.set_vote('id1', art(1), 1)
        e2, ch2 = preferences.set_vote('id1', art(1), 1)
        self.assertTrue(ch1)
        self.assertFalse(ch2)
        self.assertEqual(e1['updatedAt'], e2['updatedAt'])
        self.assertEqual(preferences.progress()['likes'], 1)

    def test_change_vote_updates_not_duplicates(self):
        preferences.set_vote('id1', art(1), 1)
        preferences.set_vote('id1', art(1), -1)
        p = preferences.progress()
        self.assertEqual((p['likes'], p['dislikes']), (0, 1))
        self.assertEqual(len(preferences.feedback()), 1)

    def test_undo_removes(self):
        preferences.set_vote('id1', art(1), 1)
        e, ch = preferences.set_vote('id1', None, 0)
        self.assertIsNone(e)
        self.assertTrue(ch)
        self.assertEqual(preferences.feedback(), {})
        self.assertEqual(preferences.set_vote('id1', None, None), (None, False))

    def test_same_canonical_url_under_new_id_replaces(self):
        preferences.set_vote('a', art(1, url='https://pub.ex/story?utm_source=rss'), 1)
        preferences.set_vote('b', art(1, url='https://pub.ex/story/'), 1)
        self.assertEqual(list(preferences.feedback()), ['b'])

    def test_ten_likes_means_ten_distinct_articles(self):
        for i in range(10):
            preferences.set_vote(f'id{i}', art(i), 1)
            preferences.set_vote(f'id{i}', art(i), 1)
        self.assertEqual(preferences.progress()['likes'], 10)

    def test_text_preserved_and_sanitised(self):
        preferences.set_vote('x', art(1, title='<b>Bold</b> &amp; title', description='<script>x()</script>Hi'), 1)
        e = preferences.feedback()['x']
        self.assertEqual(e['title'], 'Bold & title')
        self.assertEqual(e['description'], 'Hi')
        # Expiry of the article doesn't matter: entry keeps its text.
        self.assertEqual(preferences.votes_list()[0]['title'], 'Bold & title')

    def test_vote_does_not_touch_reading_state(self):
        state_path = os.environ['STATE_FILE']
        with open(state_path, 'w') as fh:
            json.dump({'readIds': ['r1'], 'later': [{'id': 's1'}]}, fh)
        preferences.set_vote('id1', art(1), 1)
        preferences.set_vote('id1', art(1), -1)
        with open(state_path) as fh:
            self.assertEqual(json.load(fh), {'readIds': ['r1'], 'later': [{'id': 's1'}]})

    def test_bad_vote_rejected(self):
        with self.assertRaises(ValueError):
            preferences.set_vote('id1', art(1), 2)
        with self.assertRaises(KeyError):
            preferences.set_vote('unknown', None, 1)

    def test_skip_is_not_a_vote_and_vote_clears_skip(self):
        preferences.add_skip('id1')
        self.assertEqual(preferences.progress()['dislikes'], 0)
        self.assertIn('id1', preferences.skipped_ids())
        preferences.set_vote('id1', art(1), 1)
        self.assertNotIn('id1', preferences.skipped_ids())

    def test_malformed_file_never_crashes(self):
        with open(os.environ['PREFERENCES_FILE'], 'w') as fh:
            fh.write('{not json')
        self.assertEqual(preferences.load()['feedback'], {})

    def test_migration_marks_existing_users_skipped(self):
        self.assertTrue(preferences.migrate_if_needed(existing_user=True))
        self.assertEqual(preferences.onboarding()['status'], 'skipped')
        self.assertFalse(preferences.migrate_if_needed(existing_user=False))
        _env.reset()
        preferences.migrate_if_needed(existing_user=False)
        self.assertEqual(preferences.onboarding()['status'], 'not_started')

    def test_topics_counted(self):
        preferences.set_vote('a', art(1, title='Solar battery boom'), 1)
        preferences.set_vote('b', art(2, title='NASA telescope'), 1)
        self.assertEqual(preferences.progress()['topics'], ['energy', 'science'])


if __name__ == '__main__':
    unittest.main()
