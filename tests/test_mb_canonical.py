"""
Tests for the MusicBrainz canonical-release lookup and the telegram bot's
0-result retry that uses it.

Mocks `_query_mb` so the test never hits the network or the DB cache
(monkey-patched cache helpers).

Run: python3 -m unittest tests.test_mb_canonical
"""
import os
# telegram_bot raises SystemExit at import if these aren't set; supply
# placeholders so the module is importable in tests.
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_ALLOWED_CHAT_ID', '0')

import unittest
from unittest import mock

from pipeline import musicbrainz as mb
from pipeline import telegram_bot as tg


# ── _extract_canonical ────────────────────────────────────────────────────────

class ExtractCanonicalTests(unittest.TestCase):

    def test_picks_highest_scoring_release(self):
        payload = {
            'releases': [
                {'score': 85, 'title': 'United', 'id': 'low-mbid',
                 'artist-credit': [{'artist': {'name': 'Marvin Gaye'}}]},
                {'score': 100, 'title': 'United', 'id': 'top-mbid',
                 'artist-credit': [
                     {'artist': {'name': 'Marvin Gaye'}, 'joinphrase': ' & '},
                     {'artist': {'name': 'Tammi Terrell'}},
                 ]},
            ],
        }
        canon = mb._extract_canonical(payload)
        self.assertEqual(canon['artist_credit'], 'Marvin Gaye & Tammi Terrell')
        self.assertEqual(canon['title'], 'United')
        self.assertEqual(canon['mbid'], 'top-mbid')
        self.assertEqual(canon['score'], 100)

    def test_drops_low_scores(self):
        payload = {'releases': [
            {'score': 50, 'title': 'X', 'artist-credit': [{'artist': {'name': 'Y'}}]},
        ]}
        self.assertIsNone(mb._extract_canonical(payload))

    def test_empty_payload(self):
        self.assertIsNone(mb._extract_canonical({}))
        self.assertIsNone(mb._extract_canonical(None))
        self.assertIsNone(mb._extract_canonical({'releases': []}))

    def test_missing_artist_or_title_skipped(self):
        payload = {'releases': [
            # no artist-credit
            {'score': 100, 'title': 'United', 'artist-credit': []},
            # no title
            {'score': 100, 'title': '',
             'artist-credit': [{'artist': {'name': 'Marvin Gaye'}}]},
        ]}
        self.assertIsNone(mb._extract_canonical(payload))

    def test_joinphrase_concatenation_with_string_pieces(self):
        # MB sometimes interleaves bare strings between dict pieces.
        payload = {'releases': [{
            'score': 100, 'title': 'Watermelon Man', 'id': 'm',
            'artist-credit': [
                {'artist': {'name': 'Mongo Santamaría'}},
                ' feat. ',
                {'artist': {'name': 'La Lupe'}},
            ],
        }]}
        canon = mb._extract_canonical(payload)
        self.assertEqual(canon['artist_credit'], 'Mongo Santamaría feat. La Lupe')


# ── lookup_canonical_release: cache + MB call coordination ────────────────────

class LookupCanonicalTests(unittest.TestCase):

    def setUp(self):
        # In-memory cache that mimics the (artist,album) -> (canon, fresh) shape.
        self._cache = {}

    def _get(self, artist, album, ttl):
        key = (artist.lower(), album.lower())
        if key not in self._cache:
            return None
        return (self._cache[key], True)  # always fresh in tests

    def _upsert(self, artist, album, canon):
        self._cache[(artist.lower(), album.lower())] = canon

    def _run_with_mocks(self, artist, album, mb_payload, mb_should_be_called=True):
        with mock.patch.object(mb.pipeline_db, 'get_canonical_cached',
                               side_effect=self._get), \
             mock.patch.object(mb.pipeline_db, 'upsert_canonical_cache',
                               side_effect=self._upsert), \
             mock.patch.object(mb, '_query_mb', return_value=mb_payload) as m:
            result = mb.lookup_canonical_release(artist, album)
        self.assertEqual(m.called, mb_should_be_called)
        return result

    def test_cache_hit_skips_network(self):
        self._cache[('marvin gaye', 'united')] = {
            'artist_credit': 'Marvin Gaye & Tammi Terrell',
            'title': 'United', 'mbid': 'cached', 'score': 100,
        }
        canon = self._run_with_mocks('Marvin Gaye', 'United', None,
                                     mb_should_be_called=False)
        self.assertEqual(canon['mbid'], 'cached')

    def test_cache_miss_queries_mb_and_stores(self):
        payload = {'releases': [{'score': 100, 'title': 'United', 'id': 'x',
            'artist-credit': [
                {'artist': {'name': 'Marvin Gaye'}, 'joinphrase': ' & '},
                {'artist': {'name': 'Tammi Terrell'}},
            ]}]}
        canon = self._run_with_mocks('Marvin Gaye', 'United', payload)
        self.assertEqual(canon['artist_credit'], 'Marvin Gaye & Tammi Terrell')
        self.assertIn(('marvin gaye', 'united'), self._cache)

    def test_negative_result_is_cached(self):
        canon = self._run_with_mocks('Nope', 'Nope', {'releases': []})
        self.assertIsNone(canon)
        self.assertIsNone(self._cache[('nope', 'nope')])

    def test_various_artists_skipped(self):
        self.assertIsNone(mb.lookup_canonical_release('Various Artists', 'X'))
        self.assertIsNone(mb.lookup_canonical_release('', 'X'))
        self.assertIsNone(mb.lookup_canonical_release('X', ''))


# ── telegram_bot._mb_retry_query: decision to retry or skip ───────────────────

class MbRetryQueryDecisionTests(unittest.TestCase):

    def test_retry_when_mb_resolves_to_different_credit(self):
        with mock.patch.object(tg.musicbrainz, 'lookup_canonical_release',
                               return_value={
                                   'artist_credit': 'Marvin Gaye & Tammi Terrell',
                                   'title': 'United', 'mbid': 'x', 'score': 100,
                               }):
            retry_q, label = tg._mb_retry_query(
                'Marvin Gaye', 'United', 'Marvin Gaye United',
                profile=tg.recover.MUSIC,
            )
        self.assertEqual(retry_q, 'Marvin Gaye & Tammi Terrell United')
        self.assertEqual(label, 'Marvin Gaye & Tammi Terrell - United')

    def test_skip_when_mb_canonical_matches_original(self):
        # MB returned the same words the user already typed — no point retrying.
        with mock.patch.object(tg.musicbrainz, 'lookup_canonical_release',
                               return_value={
                                   'artist_credit': 'Chic',
                                   'title': 'The Very Best of Chic',
                                   'mbid': 'x', 'score': 100,
                               }):
            retry_q, label = tg._mb_retry_query(
                'chic', 'the very best of chic', 'chic the very best of chic',
                profile=tg.recover.MUSIC,
            )
        self.assertIsNone(retry_q)
        self.assertIsNone(label)

    def test_skip_when_mb_returns_nothing(self):
        with mock.patch.object(tg.musicbrainz, 'lookup_canonical_release',
                               return_value=None):
            retry_q, _ = tg._mb_retry_query('X', 'Y', 'X Y',
                                            profile=tg.recover.MUSIC)
        self.assertIsNone(retry_q)

    def test_skip_for_audiobook_profile(self):
        # Should not even hit MB for audiobooks.
        with mock.patch.object(tg.musicbrainz, 'lookup_canonical_release') as m:
            retry_q, _ = tg._mb_retry_query('Stephen King', 'It', 'Stephen King It',
                                            profile=tg.recover.AUDIOBOOK)
        self.assertIsNone(retry_q)
        m.assert_not_called()

    def test_mb_exception_is_swallowed(self):
        with mock.patch.object(tg.musicbrainz, 'lookup_canonical_release',
                               side_effect=RuntimeError('boom')):
            retry_q, _ = tg._mb_retry_query('X', 'Y', 'X Y',
                                            profile=tg.recover.MUSIC)
        self.assertIsNone(retry_q)


if __name__ == '__main__':
    unittest.main()
