"""
Tests the recover.QualityProfile behavior — file scoring and folder scoring
with both MUSIC and AUDIOBOOK presets. Uses synthetic file dicts shaped
like slskd's response payloads.

Run: python3 -m unittest tests.test_quality_profile
"""
import unittest

from pipeline import recover as r


def _f(filename: str, size: int = 5_000_000, length: int = 240,
       bitDepth: int | None = None, sampleRate: int | None = None) -> dict:
    out = {'filename': filename, 'size': size, 'length': length}
    if bitDepth is not None:
        out['bitDepth'] = bitDepth
    if sampleRate is not None:
        out['sampleRate'] = sampleRate
    return out


class FileScoreTests(unittest.TestCase):

    def test_music_rejects_low_bitrate_mp3(self):
        # 64 kbps mp3 = 240s * 8000 bytes/s. 320 kbps would be 9.6 MB.
        # Use 1.92 MB for 240s = 64 kbps.
        low = _f("audiobook/part01.mp3", size=1_920_000, length=240)
        self.assertEqual(r._file_score(low, r.MUSIC), -1)

    def test_audiobook_accepts_low_bitrate_mp3(self):
        low = _f("audiobook/part01.mp3", size=1_920_000, length=240)
        self.assertGreater(r._file_score(low, r.AUDIOBOOK), 0)

    def test_audiobook_rejects_truly_garbage_bitrate(self):
        # 16 kbps mp3 — below the audiobook floor of 24
        garbage = _f("noisy/part.mp3", size=480_000, length=240)
        self.assertEqual(r._file_score(garbage, r.AUDIOBOOK), -1)

    def test_music_accepts_high_bitrate_mp3(self):
        # 320 kbps mp3
        good = _f("album/track01.mp3", size=9_600_000, length=240)
        self.assertGreater(r._file_score(good, r.MUSIC), 0)

    def test_audiobook_accepts_m4a_without_length(self):
        # m4a often comes through without `length` populated — audiobook
        # profile must still accept; music profile rejects m4a outright.
        m4a = _f("book/part01.m4a", size=10_000_000, length=0)
        self.assertGreater(r._file_score(m4a, r.AUDIOBOOK), 0)
        self.assertEqual(r._file_score(m4a, r.MUSIC), -1)

    def test_audiobook_accepts_m4b(self):
        m4b = _f("book/book.m4b", size=200_000_000, length=21600)
        self.assertGreater(r._file_score(m4b, r.AUDIOBOOK), 0)

    def test_flac_scores_under_both_profiles(self):
        flac = _f("album/track.flac", size=30_000_000, length=240,
                  bitDepth=24, sampleRate=96000)
        self.assertGreater(r._file_score(flac, r.MUSIC), 500)
        self.assertGreater(r._file_score(flac, r.AUDIOBOOK), 500)


class FindBestFolderTests(unittest.TestCase):

    def _resp(self, username: str, upload_speed: int, files: list) -> dict:
        return {'username': username, 'uploadSpeed': upload_speed, 'files': files}

    def test_audiobook_profile_picks_low_bitrate_m4a_folder(self):
        # A single audiobook folder with 6 m4a chapters, 64 kbps-ish equivalents
        ab_files = [_f(f"books/Dune/ch{i:02d}.m4a", size=80_000_000, length=3600)
                    for i in range(1, 7)]
        responses = [self._resp("slowbookpeer", 800_000, ab_files)]

        # MUSIC profile rejects (m4a not in FORMAT_SCORES, speed below 2 MB/s)
        best_music = r.find_best_folder(responses, artist="Frank Herbert", album="Dune",
                                         profile=r.MUSIC)
        self.assertIsNone(best_music)

        # AUDIOBOOK profile accepts
        best_ab = r.find_best_folder(responses, artist="Frank Herbert", album="Dune",
                                      profile=r.AUDIOBOOK)
        self.assertIsNotNone(best_ab)
        self.assertEqual(best_ab.file_count, 6)
        self.assertEqual(best_ab.fmt, 'm4a')

    def test_music_profile_unaffected_by_audiobook_changes(self):
        # Regression check: a normal FLAC album still scores under MUSIC.
        flac_files = [_f(f"funk/Vulfpeck/track{i:02d}.flac",
                         size=30_000_000, length=240, bitDepth=24, sampleRate=96000)
                      for i in range(1, 11)]
        responses = [self._resp("audiophile", 5_000_000, flac_files)]
        best = r.find_best_folder(responses, artist="Vulfpeck", album="Schvitz",
                                   profile=r.MUSIC)
        self.assertIsNotNone(best)
        self.assertEqual(best.fmt, 'flac')


if __name__ == '__main__':
    unittest.main()
