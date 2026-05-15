"""
Detector tests use the pure extract_signals() function with synthetic
file-info dicts, so the suite runs without touching mutagen or the disk.

Run: python3 -m unittest tests.test_audiobook
"""
import unittest
from pathlib import Path

from pipeline import audiobook as ab


def _fi(*, genre='', title='', artist='', album='', albumartist='',
        bitrate=320, duration=240, ext='flac') -> dict:
    return {
        'genre': genre, 'title': title, 'artist': artist, 'album': album,
        'albumartist': albumartist, 'bitrate': bitrate, 'duration': duration,
        'ext': ext,
    }


class ExtractSignalsTests(unittest.TestCase):

    def test_typical_flac_album_is_not_audiobook(self):
        files = [_fi(genre='Funk', title=f'Track {i}', bitrate=1000, duration=240)
                 for i in range(1, 11)]
        sig = ab.extract_signals(files)
        self.assertFalse(sig['is_audiobook'])

    def test_strong_genre_alone_is_enough(self):
        files = [_fi(genre='Audiobook', title='Chapter 1', ext='mp3',
                     bitrate=320, duration=240)]
        sig = ab.extract_signals(files)
        self.assertTrue(sig['is_audiobook'])
        self.assertIn('genre=strong', sig['reason'])

    def test_spoken_word_variant_matches(self):
        for g in ('Spoken Word', 'spoken-word', 'Audio Book', 'AUDIOBOOK'):
            files = [_fi(genre=g, ext='mp3', bitrate=320, duration=240)]
            with self.subTest(genre=g):
                self.assertTrue(ab.extract_signals(files)['is_audiobook'])

    def test_fiction_alone_is_not_enough(self):
        # Weak signal alone — many music albums tag genre "Fiction".
        files = [_fi(genre='Fiction', title=f'Track {i}', ext='flac',
                     bitrate=1000, duration=240) for i in range(1, 8)]
        self.assertFalse(ab.extract_signals(files)['is_audiobook'])

    def test_low_bitrate_mp3_with_long_duration_routes(self):
        # No genre, but classic audiobook fingerprint: 64 kbps mp3,
        # ~50 min per "track" — two weak signals → True.
        files = [_fi(title=f'Part {i:02d}', bitrate=64, duration=50 * 60,
                     ext='mp3', artist='Some Author')
                 for i in range(1, 8)]
        sig = ab.extract_signals(files)
        self.assertTrue(sig['is_audiobook'])

    def test_lossless_at_short_tracks_never_routes(self):
        # Even with TCON='Fiction', high-bitrate FLAC ≤ 5 min tracks
        # is music — we only have one weak signal.
        files = [_fi(genre='Fiction', title=f'Track {i}', bitrate=1100,
                     duration=300, ext='flac')
                 for i in range(1, 11)]
        self.assertFalse(ab.extract_signals(files)['is_audiobook'])

    def test_part_title_pattern_plus_fiction_routes(self):
        files = [_fi(genre='Fiction', title=f'Annihilation - Part {i:02d}',
                     bitrate=64, duration=120, ext='mp3')
                 for i in range(1, 4)]
        sig = ab.extract_signals(files)
        # genre=weak + low bitrate + title pattern → 3 weak → route.
        self.assertTrue(sig['is_audiobook'])

    def test_empty_input_returns_false(self):
        self.assertFalse(ab.extract_signals([])['is_audiobook'])

    def test_mixed_extensions_disables_bitrate_signal(self):
        # mp3 + flac in same folder: the bitrate threshold doesn't fire,
        # so a 64-kbps-mp3-and-flac mix with no other signal is treated as music.
        files = [
            _fi(title='Track 1', bitrate=64, duration=120, ext='mp3'),
            _fi(title='Track 2', bitrate=1000, duration=240, ext='flac'),
        ]
        self.assertFalse(ab.extract_signals(files)['is_audiobook'])


class DeriveAuthorTitleTests(unittest.TestCase):

    def test_uses_album_artist_when_present(self):
        infos = [{'albumartist': 'Jeff VanderMeer', 'artist': 'Ignored',
                  'album': 'Annihilation'}]
        author, title = ab.derive_author_title(Path('whatever'), infos)
        self.assertEqual(author, 'Jeff VanderMeer')
        self.assertEqual(title, 'Annihilation')

    def test_strips_volume_prefix_from_folder(self):
        author, title = ab.derive_author_title(
            Path('Vol 1 - 2014 - Annihilation'), [{'artist': 'Jeff VanderMeer'}],
        )
        self.assertEqual(author, 'Jeff VanderMeer')
        self.assertEqual(title, 'Annihilation')

    def test_falls_back_to_unknown_author(self):
        author, title = ab.derive_author_title(Path('Some Folder'), [{}])
        self.assertEqual(author, 'Unknown Author')
        self.assertEqual(title, 'Some Folder')


if __name__ == '__main__':
    unittest.main()
